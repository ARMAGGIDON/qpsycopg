param(
    [string] $Checkpoint = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Virtual environment not found: $python" }

$bootstrapEnv = @{}
$config = & $python (Join-Path $PSScriptRoot "qpsycopg_env.py") --json | ConvertFrom-Json
foreach ($item in $config.PSObject.Properties) {
    $name = $item.Name
    $previous = [Environment]::GetEnvironmentVariable($name)
    $bootstrapEnv[$name] = $previous
    if ($null -eq $previous) { [Environment]::SetEnvironmentVariable($name, [string] $item.Value) }
}

$dsn = $env:QPSYCOPG_DSN
if (-not $dsn) { throw "QPSYCOPG_DSN is not set; add it to .env or the process environment" }
if (-not $env:PSYCOPG_PG_ROOT) { throw "PSYCOPG_PG_ROOT is not set; add it to .env" }

$uv = Get-Command uv -ErrorAction Stop
$bench = Join-Path $PSScriptRoot "bench_qt_process.py"
$qt = Join-Path $PSScriptRoot "bench_qt_responsiveness.py"
$resources = Join-Path $PSScriptRoot "process_resources.py"
$runner = Join-Path $PSScriptRoot "run_with_pg_root.py"
$hostState = Join-Path $PSScriptRoot "benchmark_host_state.py"
$summary = Join-Path $PSScriptRoot "summarize_qpsycopg_benchmark.py"
$smoke = Join-Path $PSScriptRoot "smoke_qt_thread.py"

$oldImpl = $env:PSYCOPG_IMPL
$oldWorker = $env:QPSYCOPG_WORKER_IMPL
$oldIpc = $env:QPSYCOPG_IPC
$oldNative = $env:QPSYCOPG_NATIVE_IPC
$oldRecv = $env:QPSYCOPG_NATIVE_RECV
$oldWait = $env:QPSYCOPG_NATIVE_WAIT
$oldPath = $env:PATH

function Clear-QpsycopgMode {
    Remove-Item Env:QPSYCOPG_WORKER_IMPL, Env:QPSYCOPG_IPC, Env:QPSYCOPG_NATIVE_IPC,
        Env:QPSYCOPG_NATIVE_RECV, Env:QPSYCOPG_NATIVE_WAIT -ErrorAction Ignore
}

function Set-Mode([string] $mode) {
    Clear-QpsycopgMode
    if ($mode -eq "upstream" -or $mode -eq "direct") {
        $env:PSYCOPG_IMPL = "c"
        return
    }

    $env:QPSYCOPG_WORKER_IMPL = "c"
    if ($mode -eq "thread") {
        $env:PSYCOPG_IMPL = "qt-thread"
    } elseif ($mode -eq "process") {
        $env:PSYCOPG_IMPL = "qt-process"
        $env:QPSYCOPG_IPC = "pipe"
        $env:QPSYCOPG_NATIVE_IPC = "1"
        $env:QPSYCOPG_NATIVE_RECV = "0"
        $env:QPSYCOPG_NATIVE_WAIT = "0"
    } else {
        throw "unknown benchmark mode: $mode"
    }
}

function Run([string] $mode, [string] $label, [string[]] $scriptArgs, [string] $file, [string] $directory) {
    Set-Mode $mode
    $py = if ($mode -eq "upstream") { $upstreamPython } else { $forkPython }
    $stem = [IO.Path]::GetFileNameWithoutExtension($file)
    Write-Host "[$mode] $label"
    & $py $hostState > (Join-Path $directory "host-$stem-before.json")
    if ($LASTEXITCODE -ne 0) { throw "$mode host-state before $label failed with code $LASTEXITCODE" }
    & $py $runner @scriptArgs > (Join-Path $directory $file)
    $runExit = $LASTEXITCODE
    & $py $hostState > (Join-Path $directory "host-$stem-after.json")
    if ($runExit -ne 0) { throw "$mode $label failed with code $runExit" }
    if ($LASTEXITCODE -ne 0) { throw "$mode host-state after $label failed with code $LASTEXITCODE" }
}

function Run-Pair(
    [string] $mode,
    [string] $kind,
    [int] $round,
    [int] $position,
    [int] $total,
    [string[]] $scriptArgs,
    [string] $directory
) {
    $n = "{0:D2}" -f $round
    $upstreamFile = "$kind-$n-$mode-upstream.json"
    $modeFile = "$kind-$n-$mode.json"
    $upstreamFirst = (($round + $position) % 2 -eq 0)
    if ($upstreamFirst) {
        Run "upstream" "$kind $round/$total paired with $mode" $scriptArgs $upstreamFile $directory
        Run $mode "$kind $round/$total" $scriptArgs $modeFile $directory
    } else {
        Run $mode "$kind $round/$total" $scriptArgs $modeFile $directory
        Run "upstream" "$kind $round/$total paired with $mode" $scriptArgs $upstreamFile $directory
    }
}

try {
    Write-Host "[upstream] resolve comparison revision"
    & git -C $repoRoot fetch --quiet https://github.com/psycopg/psycopg.git master
    if ($LASTEXITCODE -ne 0) { throw "failed to fetch psycopg/psycopg master" }
    $upstreamHead = (& git -C $repoRoot rev-parse FETCH_HEAD).Trim()
    $upstreamBase = (& git -C $repoRoot merge-base HEAD $upstreamHead).Trim()
    if (-not $upstreamBase) { throw "couldn't resolve upstream merge-base" }

    $pythonVersion = (& $python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))").Trim()
    $basePython = (& $python -c "import sys; print(sys._base_executable)").Trim()
    $qtVersion = (& $python -c "import PySide6; print(PySide6.__version__)").Trim()
    $psutilVersion = (& $python -c "import psutil; print(psutil.__version__)").Trim()
    $platform = (& $python -c "import platform, psutil; print(platform.platform() + '|' + platform.machine() + '|' + str(psutil.cpu_count(logical=True)))").Trim()
    $pgBin = Join-Path $env:PSYCOPG_PG_ROOT "bin"
    $pgConfig = Join-Path $pgBin "pg_config.exe"
    if (-not (Test-Path $pgConfig)) { throw "pg_config.exe not found: $pgConfig" }
    $libpqVersion = (& $pgConfig --version).Trim()

    $hashText = (@($bench, $qt, $resources, $runner, $hostState, $summary, $PSCommandPath) | ForEach-Object {
        (Get-FileHash $_ -Algorithm SHA256).Hash
    }) -join "|"
    $hashBytes = [Text.Encoding]::UTF8.GetBytes($hashText)
    $benchmarkHash = ([Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($hashBytes))).ToLower()
    $environmentText = "$upstreamBase|$pythonVersion|$libpqVersion|$qtVersion|$psutilVersion|$platform|$benchmarkHash"
    $environmentBytes = [Text.Encoding]::UTF8.GetBytes($environmentText)
    $environmentHash = ([Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($environmentBytes))).ToLower()
    $key = "{0}-{1}" -f $upstreamBase.Substring(0, 12), $environmentHash.Substring(0, 12)

    $root = Join-Path $repoRoot "test-results\qpsycopg-benchmark"
    $referenceDir = Join-Path $root "reference-$key"
    $upstreamRoot = Join-Path $root ("upstream-" + $upstreamBase.Substring(0, 12))
    $upstreamVenv = Join-Path $upstreamRoot ".venv-bench"
    $forkVenv = Join-Path $root ".venv-fork-bench"
    $upstreamPython = Join-Path $upstreamVenv "Scripts\python.exe"
    $forkPython = Join-Path $forkVenv "Scripts\python.exe"
    New-Item -ItemType Directory -Path $root -Force | Out-Null

    if (-not (Test-Path (Join-Path $upstreamRoot ".git"))) {
        Write-Host "[upstream] checkout $upstreamBase"
        & git -C $repoRoot worktree add --detach $upstreamRoot $upstreamBase
        if ($LASTEXITCODE -ne 0) { throw "failed to create upstream worktree" }
    }
    if (-not (Test-Path $upstreamPython)) {
        Write-Host "[upstream] create benchmark venv"
        & $uv.Source venv $upstreamVenv --python $basePython
        if ($LASTEXITCODE -ne 0) { throw "failed to create upstream benchmark venv" }
    }
    if (-not (Test-Path $forkPython)) {
        Write-Host "[fork] create benchmark venv"
        & $uv.Source venv $forkVenv --python $basePython
        if ($LASTEXITCODE -ne 0) { throw "failed to create fork benchmark venv" }
    }

    # Old upstream psycopg-c revisions discover PostgreSQL only through pg_config on PATH.
    # Expose the same local PostgreSQL toolchain to both builds, then restore PATH before measurements.
    $env:PATH = "$pgBin;$oldPath"
    Write-Host "[upstream] rebuild pristine environment against local libpq"
    & $uv.Source pip install --python $upstreamPython --reinstall (Join-Path $upstreamRoot "psycopg") `
        (Join-Path $upstreamRoot "psycopg_c") "psutil==$psutilVersion" "PySide6-Essentials==$qtVersion"
    if ($LASTEXITCODE -ne 0) { throw "failed to install upstream benchmark environment" }

    Write-Host "[fork] rebuild isolated environment against the same local libpq"
    & $uv.Source pip install --python $forkPython --reinstall (Join-Path $repoRoot "psycopg") `
        (Join-Path $repoRoot "psycopg_c") "psutil==$psutilVersion" "PySide6-Essentials==$qtVersion"
    if ($LASTEXITCODE -ne 0) { throw "failed to install fork benchmark environment" }
    $env:PATH = $oldPath

    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    $commit = (& git -C $repoRoot rev-parse HEAD).Trim()
    if (-not $Checkpoint) { $Checkpoint = $commit.Substring(0, 12) }
    $safeCheckpoint = $Checkpoint -replace '[^A-Za-z0-9._-]', '-'
    $outDir = Join-Path $root "checkpoint-$timestamp-$safeCheckpoint"
    $archive = Join-Path $repoRoot "test-results\qpsycopg-benchmark-$timestamp-$safeCheckpoint.zip"
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null

    Set-Mode "thread"
    Write-Host "[thread] smoke"
    & $forkPython $runner $smoke $dsn 2>&1 | Tee-Object -FilePath (Join-Path $outDir "smoke-thread.log")
    if ($LASTEXITCODE -ne 0) { throw "qt-thread smoke failed with code $LASTEXITCODE" }

    $benchArgs = @(
        $bench, $dsn,
        "--iterations", "5000",
        "--warmup-iterations", "200",
        "--rows", "100000",
        "--threads", "8",
        "--thread-batches", "20",
        "--connect-iterations", "40",
        "--cancel-iterations", "20"
    )
    $qtArgs = @($qt, $dsn, "--caller", "thread", "--queries", "60")
    $modeOrders = @(
        @("direct", "thread", "process"),
        @("thread", "process", "direct"),
        @("process", "direct", "thread")
    )
    $sqlRepeats = 6
    $qtRepeats = 5

    for ($i = 1; $i -le $sqlRepeats; $i++) {
        $position = 0
        foreach ($mode in $modeOrders[($i - 1) % 3]) {
            Run-Pair $mode "bench" $i $position $sqlRepeats $benchArgs $outDir
            $position++
        }
    }
    for ($i = 1; $i -le $qtRepeats; $i++) {
        $position = 0
        foreach ($mode in $modeOrders[($i - 1) % 3]) {
            Run-Pair $mode "qt" $i $position $qtRepeats $qtArgs $outDir
            $position++
        }
    }

    [ordered]@{
        timestamp = (Get-Date).ToString("o")
        checkpoint = $Checkpoint
        branch = (& git -C $repoRoot branch --show-current).Trim()
        commit = $commit
        reference_key = $key
        upstream_head = $upstreamHead
        upstream_base = $upstreamBase
        python = $pythonVersion
        libpq = $libpqVersion
        PySide6 = $qtVersion
        psutil = $psutilVersion
        platform = $platform
        benchmark_hash = $benchmarkHash
        methodology = "adjacent paired upstream per fork mode; pair order alternates"
        environment = "isolated non-editable upstream/fork benchmark venvs"
        repeats = $sqlRepeats
        qt_repeats = $qtRepeats
        iterations = 5000
        warmup_iterations = 200
        rows = 100000
        threads = 8
        thread_batches = 20
        connect_iterations = 40
        cancel_iterations = 20
        qt_queries = 60
        upstream_impl = "c"
        direct_impl = "c"
        worker_impl = "c"
        thread_impl = "qt-thread"
        process_impl = "qt-process"
        process_ipc = "pipe"
        process_parent_native = $true
    } | ConvertTo-Json | Set-Content (Join-Path $outDir "manifest.json") -Encoding utf8

    if (-not (Test-Path (Join-Path $referenceDir "manifest.json"))) {
        Write-Host "[reference] save first adjacent-paired upstream run as historical drift reference"
        New-Item -ItemType Directory -Path $referenceDir -Force | Out-Null
        Copy-Item (Join-Path $outDir "bench-*-*-upstream.json") $referenceDir
        Copy-Item (Join-Path $outDir "qt-*-*-upstream.json") $referenceDir
        Copy-Item (Join-Path $outDir "host-*-upstream-*.json") $referenceDir
        [ordered]@{
            timestamp = (Get-Date).ToString("o")
            reference_key = $key
            upstream_base = $upstreamBase
            source_checkpoint = $Checkpoint
            source_commit = $commit
            python = $pythonVersion
            libpq = $libpqVersion
            PySide6 = $qtVersion
            psutil = $psutilVersion
            platform = $platform
            benchmark_hash = $benchmarkHash
            methodology = "adjacent paired upstream per fork mode"
        } | ConvertTo-Json | Set-Content (Join-Path $referenceDir "manifest.json") -Encoding utf8
    } else {
        Write-Host "[reference] historical upstream reference: $referenceDir"
    }

    Write-Host "[summary] adjacent-paired current upstream vs fork + historical drift"
    & $forkPython $summary $referenceDir $outDir
    if ($LASTEXITCODE -ne 0) { throw "benchmark summary failed with code $LASTEXITCODE" }

    Compress-Archive -Path (Join-Path $outDir "*") -DestinationPath $archive -CompressionLevel Optimal -Force
    Write-Host "benchmark archive: $archive"
    Write-Host "comparison table: $(Join-Path $outDir 'comparison.md')"
} finally {
    $env:PATH = $oldPath
    if ($null -eq $oldImpl) { Remove-Item Env:PSYCOPG_IMPL -ErrorAction Ignore } else { $env:PSYCOPG_IMPL = $oldImpl }
    if ($null -eq $oldWorker) { Remove-Item Env:QPSYCOPG_WORKER_IMPL -ErrorAction Ignore } else { $env:QPSYCOPG_WORKER_IMPL = $oldWorker }
    if ($null -eq $oldIpc) { Remove-Item Env:QPSYCOPG_IPC -ErrorAction Ignore } else { $env:QPSYCOPG_IPC = $oldIpc }
    if ($null -eq $oldNative) { Remove-Item Env:QPSYCOPG_NATIVE_IPC -ErrorAction Ignore } else { $env:QPSYCOPG_NATIVE_IPC = $oldNative }
    if ($null -eq $oldRecv) { Remove-Item Env:QPSYCOPG_NATIVE_RECV -ErrorAction Ignore } else { $env:QPSYCOPG_NATIVE_RECV = $oldRecv }
    if ($null -eq $oldWait) { Remove-Item Env:QPSYCOPG_NATIVE_WAIT -ErrorAction Ignore } else { $env:QPSYCOPG_NATIVE_WAIT = $oldWait }
    foreach ($entry in $bootstrapEnv.GetEnumerator()) {
        if ($null -eq $entry.Value) { Remove-Item "Env:$($entry.Key)" -ErrorAction Ignore }
        else { [Environment]::SetEnvironmentVariable($entry.Key, [string] $entry.Value) }
    }
}
