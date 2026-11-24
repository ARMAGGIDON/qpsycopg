$ErrorActionPreference = "Stop"
$repoRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$dsn = $env:QPSYCOPG_DSN
if (-not (Test-Path $python)) { throw "Virtual environment not found: $python" }
if (-not $dsn) { throw "QPSYCOPG_DSN is not set" }

$uv = Get-Command uv -ErrorAction Stop
Write-Host "[build] psycopg-c native IPC"
& $uv.Source pip install --python $python --reinstall --no-deps -e (Join-Path $repoRoot "psycopg_c")
if ($LASTEXITCODE -ne 0) { throw "psycopg-c rebuild failed with code $LASTEXITCODE" }

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$outDir = Join-Path $repoRoot "test-results\native-ipc-validation-$timestamp"
$archive = Join-Path $repoRoot "test-results\qpsycopg-native-ipc-validation-$timestamp.zip"
New-Item -ItemType Directory -Path $outDir -Force | Out-Null

$oldImpl = $env:PSYCOPG_IMPL
$oldWorker = $env:QPSYCOPG_WORKER_IMPL
$oldIpc = $env:QPSYCOPG_IPC
$oldNative = $env:QPSYCOPG_NATIVE_IPC
$oldProfile = $env:QPSYCOPG_PROFILE_PHASES
$bench = Join-Path $PSScriptRoot "bench_qt_process.py"
$qt = Join-Path $PSScriptRoot "bench_qt_responsiveness.py"
$cpu = Join-Path $PSScriptRoot "profile_cpu_phases.py"
$workerProfile = Join-Path $PSScriptRoot "profile_worker_hot_path.py"
$smoke = Join-Path $PSScriptRoot "smoke_qt_process.py"

function Set-Qpsycopg([bool] $native) {
    $env:PSYCOPG_IMPL = "qt-process"
    $env:QPSYCOPG_WORKER_IMPL = "binary"
    $env:QPSYCOPG_IPC = "pipe"
    $env:QPSYCOPG_NATIVE_IPC = if ($native) { "1" } else { "0" }
}

function Run([bool] $native, [string] $label, [string[]] $pythonArgs, [string] $file) {
    $mode = if ($native) { "native" } else { "python" }
    Write-Host "[$mode] $label"
    Set-Qpsycopg $native
    & $python @pythonArgs > (Join-Path $outDir $file)
    if ($LASTEXITCODE -ne 0) { throw "$mode $label failed with code $LASTEXITCODE" }
}

try {
    Set-Qpsycopg $true
    & $python $smoke $dsn *> (Join-Path $outDir "smoke-native.log")
    if ($LASTEXITCODE -ne 0) { throw "native smoke failed with code $LASTEXITCODE" }

    $benchArgs = @($bench, $dsn, "--iterations", "500", "--rows", "100000", "--threads", "8")
    $qtArgs = @($qt, $dsn, "--caller", "thread")

    for ($i = 1; $i -le 5; $i++) {
        $n = "{0:D2}" -f $i
        $order = if ($i % 2) { @($false, $true) } else { @($true, $false) }
        foreach ($native in $order) {
            $mode = if ($native) { "native" } else { "python" }
            Run $native "SQL $i/5" $benchArgs "bench-$n-$mode.json"
        }
    }

    for ($i = 1; $i -le 3; $i++) {
        $n = "{0:D2}" -f $i
        $order = if ($i % 2) { @($false, $true) } else { @($true, $false) }
        foreach ($native in $order) {
            $mode = if ($native) { "native" } else { "python" }
            Run $native "Qt $i/3" $qtArgs "qt-$n-$mode.json"
        }
    }

    Run $false "CPU phases" @($cpu, $dsn) "cpu-phases-python.json"
    Run $true "CPU phases" @($cpu, $dsn) "cpu-phases-native.json"

    $env:QPSYCOPG_PROFILE_PHASES = "1"
    Run $false "worker phases" @($workerProfile, $dsn) "worker-phases-python.json"
    Run $true "worker phases" @($workerProfile, $dsn) "worker-phases-native.json"

    [ordered]@{
        timestamp = (Get-Date).ToString("o")
        branch = (& git -C $repoRoot branch --show-current).Trim()
        commit = (& git -C $repoRoot rev-parse HEAD).Trim()
        sql_repeats = 5
        qt_repeats = 3
        iterations = 500
        worker_profile_iterations = 10000
        rows = 100000
        threads = 8
        ipc = "pipe"
    } | ConvertTo-Json | Set-Content (Join-Path $outDir "manifest.json") -Encoding utf8

    Compress-Archive -Path (Join-Path $outDir "*") -DestinationPath $archive -CompressionLevel Optimal -Force
    Remove-Item $outDir -Recurse -Force
    Write-Host "Native IPC validation archive: $archive"
} finally {
    if ($null -eq $oldImpl) { Remove-Item Env:PSYCOPG_IMPL -ErrorAction Ignore } else { $env:PSYCOPG_IMPL = $oldImpl }
    if ($null -eq $oldWorker) { Remove-Item Env:QPSYCOPG_WORKER_IMPL -ErrorAction Ignore } else { $env:QPSYCOPG_WORKER_IMPL = $oldWorker }
    if ($null -eq $oldIpc) { Remove-Item Env:QPSYCOPG_IPC -ErrorAction Ignore } else { $env:QPSYCOPG_IPC = $oldIpc }
    if ($null -eq $oldNative) { Remove-Item Env:QPSYCOPG_NATIVE_IPC -ErrorAction Ignore } else { $env:QPSYCOPG_NATIVE_IPC = $oldNative }
    if ($null -eq $oldProfile) { Remove-Item Env:QPSYCOPG_PROFILE_PHASES -ErrorAction Ignore } else { $env:QPSYCOPG_PROFILE_PHASES = $oldProfile }
}
