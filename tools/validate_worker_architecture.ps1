$ErrorActionPreference = "Stop"
$repoRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$dsn = $env:QPSYCOPG_DSN
if (-not (Test-Path $python)) { throw "Virtual environment not found: $python" }
if (-not $dsn) { throw "QPSYCOPG_DSN is not set" }

$uv = Get-Command uv -ErrorAction Stop
Write-Host "[build] psycopg-c"
& $uv.Source pip install --python $python --reinstall --no-deps -e (Join-Path $repoRoot "psycopg_c")
if ($LASTEXITCODE -ne 0) { throw "psycopg-c rebuild failed with code $LASTEXITCODE" }

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$outDir = Join-Path $repoRoot "test-results\worker-architecture-$timestamp"
$archive = Join-Path $repoRoot "test-results\qpsycopg-worker-architecture-$timestamp.zip"
New-Item -ItemType Directory -Path $outDir -Force | Out-Null

$oldImpl = $env:PSYCOPG_IMPL
$oldWorker = $env:QPSYCOPG_WORKER_IMPL
$oldIpc = $env:QPSYCOPG_IPC
$oldNative = $env:QPSYCOPG_NATIVE_IPC
$oldRecv = $env:QPSYCOPG_NATIVE_RECV
$oldWait = $env:QPSYCOPG_NATIVE_WAIT
$bench = Join-Path $PSScriptRoot "bench_worker_architecture.py"

function Set-Mode([string] $mode) {
    if ($mode -eq "process") {
        $env:PSYCOPG_IMPL = "qt-process"
        $env:QPSYCOPG_WORKER_IMPL = "binary"
        $env:QPSYCOPG_IPC = "pipe"
        $env:QPSYCOPG_NATIVE_IPC = "1"
        $env:QPSYCOPG_NATIVE_RECV = "0"
        $env:QPSYCOPG_NATIVE_WAIT = "0"
    } else {
        $env:PSYCOPG_IMPL = "binary"
        Remove-Item Env:QPSYCOPG_WORKER_IMPL, Env:QPSYCOPG_IPC, Env:QPSYCOPG_NATIVE_IPC,
            Env:QPSYCOPG_NATIVE_RECV, Env:QPSYCOPG_NATIVE_WAIT -ErrorAction Ignore
    }
}

function Run([string] $mode, [string] $file) {
    Set-Mode $mode
    $benchMode = if ($mode -eq "thread") { "thread" } else { "direct" }
    Write-Host "[$mode] architecture benchmark"
    & $python $bench $dsn --mode $benchMode --iterations 2000 --warmup 200 --rows 100000 > (Join-Path $outDir $file)
    if ($LASTEXITCODE -ne 0) { throw "$mode benchmark failed with code $LASTEXITCODE" }
}

try {
    $orders = @(
        @("direct", "thread", "process"),
        @("thread", "process", "direct"),
        @("process", "direct", "thread")
    )
    for ($i = 1; $i -le 6; $i++) {
        $n = "{0:D2}" -f $i
        foreach ($mode in $orders[($i - 1) % 3]) { Run $mode "bench-$n-$mode.json" }
    }

    [ordered]@{
        timestamp = (Get-Date).ToString("o")
        branch = (& git -C $repoRoot branch --show-current).Trim()
        commit = (& git -C $repoRoot rev-parse HEAD).Trim()
        repeats = 6
        iterations = 2000
        warmup = 200
        rows = 100000
        modes = @("direct", "thread", "process")
        process_ipc = "pipe"
        process_parent_native = $true
        process_worker_native_recv = $false
        process_worker_native_wait = $false
    } | ConvertTo-Json | Set-Content (Join-Path $outDir "manifest.json") -Encoding utf8

    Compress-Archive -Path (Join-Path $outDir "*") -DestinationPath $archive -CompressionLevel Optimal -Force
    Remove-Item $outDir -Recurse -Force
    Write-Host "Worker architecture archive: $archive"
} finally {
    if ($null -eq $oldImpl) { Remove-Item Env:PSYCOPG_IMPL -ErrorAction Ignore } else { $env:PSYCOPG_IMPL = $oldImpl }
    if ($null -eq $oldWorker) { Remove-Item Env:QPSYCOPG_WORKER_IMPL -ErrorAction Ignore } else { $env:QPSYCOPG_WORKER_IMPL = $oldWorker }
    if ($null -eq $oldIpc) { Remove-Item Env:QPSYCOPG_IPC -ErrorAction Ignore } else { $env:QPSYCOPG_IPC = $oldIpc }
    if ($null -eq $oldNative) { Remove-Item Env:QPSYCOPG_NATIVE_IPC -ErrorAction Ignore } else { $env:QPSYCOPG_NATIVE_IPC = $oldNative }
    if ($null -eq $oldRecv) { Remove-Item Env:QPSYCOPG_NATIVE_RECV -ErrorAction Ignore } else { $env:QPSYCOPG_NATIVE_RECV = $oldRecv }
    if ($null -eq $oldWait) { Remove-Item Env:QPSYCOPG_NATIVE_WAIT -ErrorAction Ignore } else { $env:QPSYCOPG_NATIVE_WAIT = $oldWait }
}
