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
$summary = Join-Path $PSScriptRoot "summarize_qpsycopg_benchmark.py"
$smoke = Join-Path $PSScriptRoot "smoke_qt_thread.py"

$oldImpl = $env:PSYCOPG_IMPL
$oldWorker = $env:QPSYCOPG_WORKER_IMPL
$oldIpc = $env:QPSYCOPG_IPC
$oldNative = $env:QPSYCOPG_NATIVE_IPC
$oldRecv = $env:QPSYCOPG_NATIVE_RECV
$oldWait = $env:QPSYCOPG_NATIVE_WAIT

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

function Run([string] $py, [string] $mode, [string] $label, [string[]] $scriptArgs, [string] $file, [string] $directory) {
    Set-Mode $mode
    Write-Host "[$mode] $label"
    & $py $runner @scriptArgs > (Join-Path $directory $file)
    if ($LASTEXITCODE -ne 0) { throw "$mode $label failed with code $LASTEXITCODE" }
}

try {
    Write-Host "[upstream] resolve frozen baseline"
    & git -C $repoRoot fetch --quiet https://github.com/psycopg/psycopg.git master
    if ($LASTEXITCODE -ne 0) { throw "failed to fetch psycopg/psycopg master" }
    $upstreamHead = (& git -C $repoRoot rev-parse FETCH_HEAD).Trim()
    $upstreamBase = (& git -C $repoRoot merge-base HEAD $upstreamHead).Trim()
    if (-not $upstreamBase) { throw "couldn't resolve upstream merge-base" }

    $pythonVersion = (& $python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))").Trim()
    $basePython = (& $python -c "import sys; print(sys._base_executable)").Trim()
    $qtVersion = (& $python -c "import PySide6; print(PySide6.__version__)").Trim()
    $psutilVersion = (& $python -c "import psutil; print(psutil.__version__)").Trim()
    $pgConfig = Join-Path $env:PSYCOPG_PG_ROOT "bin\pg_config.exe"
    $libpqVersion = if (Test-Path $pgConfig) { (& $pgConfig --version).Trim() } else { "unknown" }

    $hashText = (@($bench, $qt, $resources, $runner) | ForEach-Object {
        (Get-FileHash $_ -Algorithm SHA256).Hash
    }) -join "|"
    $hashBytes = [Text.Encoding]::UTF8.GetBytes($hashText)
    $benchmarkHash = ([Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($hashBytes))).ToLower()
    $key = "{0}-py{1}-{2}" -f $upstreamBase.Substring(0, 12), $pythonVersion.Replace(".", ""), $benchmarkHash.Substring(0, 12)

    $root = Join-Path $repoRoot "test-results\qpsycopg-benchmark"
    $baselineDir = Join-Path $root "baseline-$key"
    $upstreamRoot = Join-Path $root ("upstream-" + $upstreamBase.Substring(0, 12))
    $upstreamVenv = Join-Path $upstreamRoot ".venv-bench"
    $upstreamPython = Join-Path $upstreamVenv "Scripts\python.exe"
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

    if (-not (Test-Path (Join-Path $baselineDir "manifest.json"))) {
        Write-Host "[upstream] build pristine baseline environment"
        & $uv.Source pip install --python $upstreamPython --reinstall -e (Join-Path $upstreamRoot "psycopg") `
            -e (Join-Path $upstreamRoot "psycopg_c") "psutil==$psutilVersion" "PySide6-Essentials==$qtVersion"
        if ($LASTEXITCODE -ne 0) { throw "failed to install upstream benchmark environment" }

        New-Item -ItemType Directory -Path $baselineDir -Force | Out-Null
        $benchArgs = @($bench, $dsn, "--iterations", "1000", "--rows", "100000", "--threads", "8")
        $qtArgs = @($qt, $dsn, "--caller", "thread")
        for ($i = 1; $i -le 6; $i++) {
            $n = "{0:D2}" -f $i
            Run $upstreamPython "upstream" "SQL $i/6" $benchArgs "bench-$n-upstream.json" $baselineDir
        }
        for ($i = 1; $i -le 3; $i++) {
            $n = "{0:D2}" -f $i
            Run $upstreamPython "upstream" "Qt $i/3" $qtArgs "qt-$n-upstream.json" $baselineDir
        }

        [ordered]@{
            timestamp = (Get-Date).ToString("o")
            repository = "psycopg/psycopg"
            upstream_head = $upstreamHead
            upstream_base = $upstreamBase
            python = $pythonVersion
            libpq = $libpqVersion
            PySide6 = $qtVersion
            psutil = $psutilVersion
            benchmark_hash = $benchmarkHash
            repeats = 6
            qt_repeats = 3
            iterations = 1000
            rows = 100000
            threads = 8
            impl = "c"
        } | ConvertTo-Json | Set-Content (Join-Path $baselineDir "manifest.json") -Encoding utf8
        Write-Host "[upstream] baseline saved: $baselineDir"
    } else {
        Write-Host "[upstream] reuse baseline: $baselineDir"
    }

    Write-Host "[fork] rebuild psycopg-c against the same local libpq"
    & $uv.Source pip install --python $python --reinstall --no-deps -e (Join-Path $repoRoot "psycopg_c")
    if ($LASTEXITCODE -ne 0) { throw "fork psycopg-c rebuild failed with code $LASTEXITCODE" }

    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    $commit = (& git -C $repoRoot rev-parse HEAD).Trim()
    if (-not $Checkpoint) { $Checkpoint = $commit.Substring(0, 12) }
    $safeCheckpoint = $Checkpoint -replace '[^A-Za-z0-9._-]', '-'
    $outDir = Join-Path $root "checkpoint-$timestamp-$safeCheckpoint"
    $archive = Join-Path $repoRoot "test-results\qpsycopg-benchmark-$timestamp-$safeCheckpoint.zip"
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null

    Set-Mode "thread"
    Write-Host "[thread] smoke"
    & $python $runner $smoke $dsn 2>&1 | Tee-Object -FilePath (Join-Path $outDir "smoke-thread.log")
    if ($LASTEXITCODE -ne 0) { throw "qt-thread smoke failed with code $LASTEXITCODE" }

    $benchArgs = @($bench, $dsn, "--iterations", "1000", "--rows", "100000", "--threads", "8")
    $qtArgs = @($qt, $dsn, "--caller", "thread")
    $orders = @(
        @("direct", "thread", "process"),
        @("thread", "process", "direct"),
        @("process", "direct", "thread")
    )
    for ($i = 1; $i -le 6; $i++) {
        $n = "{0:D2}" -f $i
        foreach ($mode in $orders[($i - 1) % 3]) {
            Run $python $mode "SQL $i/6" $benchArgs "bench-$n-$mode.json" $outDir
        }
    }
    for ($i = 1; $i -le 3; $i++) {
        $n = "{0:D2}" -f $i
        foreach ($mode in $orders[$i - 1]) {
            Run $python $mode "Qt $i/3" $qtArgs "qt-$n-$mode.json" $outDir
        }
    }

    [ordered]@{
        timestamp = (Get-Date).ToString("o")
        checkpoint = $Checkpoint
        branch = (& git -C $repoRoot branch --show-current).Trim()
        commit = $commit
        baseline_key = $key
        upstream_base = $upstreamBase
        python = $pythonVersion
        libpq = $libpqVersion
        PySide6 = $qtVersion
        psutil = $psutilVersion
        benchmark_hash = $benchmarkHash
        repeats = 6
        qt_repeats = 3
        iterations = 1000
        rows = 100000
        threads = 8
        direct_impl = "c"
        worker_impl = "c"
        thread_impl = "qt-thread"
        process_impl = "qt-process"
        process_ipc = "pipe"
        process_parent_native = $true
    } | ConvertTo-Json | Set-Content (Join-Path $outDir "manifest.json") -Encoding utf8

    Write-Host "[summary] upstream vs fork"
    & $python $summary $baselineDir $outDir
    if ($LASTEXITCODE -ne 0) { throw "benchmark summary failed with code $LASTEXITCODE" }

    Compress-Archive -Path (Join-Path $outDir "*") -DestinationPath $archive -CompressionLevel Optimal -Force
    Write-Host "benchmark archive: $archive"
    Write-Host "comparison table: $(Join-Path $outDir 'comparison.md')"
} finally {
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
