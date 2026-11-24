$ErrorActionPreference = "Stop"
$repoRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$dsn = $env:QPSYCOPG_DSN
if (-not (Test-Path $python)) { throw "Virtual environment not found: $python" }
if (-not $dsn) { throw "QPSYCOPG_DSN is not set" }

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$outDir = Join-Path $repoRoot "test-results\worker-qt-cancel-$timestamp"
$archive = Join-Path $repoRoot "test-results\qpsycopg-worker-qt-cancel-$timestamp.zip"
New-Item -ItemType Directory -Path $outDir -Force | Out-Null

$oldImpl = $env:PSYCOPG_IMPL
$oldWorker = $env:QPSYCOPG_WORKER_IMPL
$oldIpc = $env:QPSYCOPG_IPC
$oldNative = $env:QPSYCOPG_NATIVE_IPC
$oldRecv = $env:QPSYCOPG_NATIVE_RECV
$oldWait = $env:QPSYCOPG_NATIVE_WAIT
$bench = Join-Path $PSScriptRoot "bench_worker_qt_cancel.py"

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

function Run([string] $mode, [string] $caller, [string] $file) {
    Set-Mode $mode
    $benchMode = if ($mode -eq "thread") { "thread" } else { "direct" }
    Write-Host "[$mode/$caller] Qt + cancellation"
    & $python $bench $dsn --mode $benchMode --caller $caller --queries 20 --sleep-ms 25 --timer-ms 10 --cancel-delay .2 `
        > (Join-Path $outDir $file)
    if ($LASTEXITCODE -ne 0) { throw "$mode/$caller validation failed with code $LASTEXITCODE" }
}

try {
    $orders = @(
        @("direct", "thread", "process"),
        @("thread", "process", "direct"),
        @("process", "direct", "thread")
    )
    foreach ($caller in @("thread", "main")) {
        for ($i = 1; $i -le 3; $i++) {
            $n = "{0:D2}" -f $i
            foreach ($mode in $orders[$i - 1]) { Run $mode $caller "qt-cancel-$caller-$n-$mode.json" }
        }
    }

    [ordered]@{
        timestamp = (Get-Date).ToString("o")
        branch = (& git -C $repoRoot branch --show-current).Trim()
        commit = (& git -C $repoRoot rev-parse HEAD).Trim()
        repeats = 3
        modes = @("direct", "thread", "process")
        callers = @("thread", "main")
        qt_queries = 20
        qt_sleep_ms = 25
        qt_timer_ms = 10
        cancel_delay_seconds = .2
    } | ConvertTo-Json | Set-Content (Join-Path $outDir "manifest.json") -Encoding utf8

    Compress-Archive -Path (Join-Path $outDir "*") -DestinationPath $archive -CompressionLevel Optimal -Force
    Remove-Item $outDir -Recurse -Force
    Write-Host "Worker Qt/cancel archive: $archive"
} finally {
    if ($null -eq $oldImpl) { Remove-Item Env:PSYCOPG_IMPL -ErrorAction Ignore } else { $env:PSYCOPG_IMPL = $oldImpl }
    if ($null -eq $oldWorker) { Remove-Item Env:QPSYCOPG_WORKER_IMPL -ErrorAction Ignore } else { $env:QPSYCOPG_WORKER_IMPL = $oldWorker }
    if ($null -eq $oldIpc) { Remove-Item Env:QPSYCOPG_IPC -ErrorAction Ignore } else { $env:QPSYCOPG_IPC = $oldIpc }
    if ($null -eq $oldNative) { Remove-Item Env:QPSYCOPG_NATIVE_IPC -ErrorAction Ignore } else { $env:QPSYCOPG_NATIVE_IPC = $oldNative }
    if ($null -eq $oldRecv) { Remove-Item Env:QPSYCOPG_NATIVE_RECV -ErrorAction Ignore } else { $env:QPSYCOPG_NATIVE_RECV = $oldRecv }
    if ($null -eq $oldWait) { Remove-Item Env:QPSYCOPG_NATIVE_WAIT -ErrorAction Ignore } else { $env:QPSYCOPG_NATIVE_WAIT = $oldWait }
}
