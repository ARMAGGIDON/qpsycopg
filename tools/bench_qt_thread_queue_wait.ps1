param(
    [int] $Pairs = 8,
    [int] $Iterations = 4000,
    [int] $Warmup = 300,
    [double] $WatchdogSeconds = 8
)

$ErrorActionPreference = "Stop"
if ($Pairs -lt 4 -or $Iterations -lt 100 -or $Warmup -lt 0 -or $WatchdogSeconds -lt 2) {
    throw "Expected Pairs>=4, Iterations>=100, Warmup>=0, WatchdogSeconds>=2"
}

$repoRoot = Split-Path $PSScriptRoot -Parent
$bootstrapPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$forkPython = Join-Path $repoRoot "test-results\qpsycopg-benchmark\.venv-fork-bench\Scripts\python.exe"
$configScript = Join-Path $PSScriptRoot "qpsycopg_env.py"
$bootstrap = Join-Path $PSScriptRoot "run_with_pg_root.py"
$probe = Join-Path $PSScriptRoot "bench_qt_thread_queue_wait.py"
foreach ($path in @($bootstrapPython, $forkPython, $configScript, $bootstrap, $probe)) {
    if (-not (Test-Path $path)) {
        throw "Missing diagnostic prerequisite: $path. Reuse the existing v3 benchmark venv."
    }
}

$originalImpl = $env:PSYCOPG_IMPL
$originalWorker = $env:QPSYCOPG_WORKER_IMPL
$addedConfig = @{}
try {
    $config = & $bootstrapPython $configScript --json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "Cannot load qpsycopg .env" }
    foreach ($entry in $config.PSObject.Properties) {
        $previous = [Environment]::GetEnvironmentVariable($entry.Name)
        $addedConfig[$entry.Name] = $previous
        if ($null -eq $previous) {
            [Environment]::SetEnvironmentVariable($entry.Name, [string] $entry.Value)
        }
    }

    if (-not $env:QPSYCOPG_DSN) { throw "QPSYCOPG_DSN missing from .env" }
    if (-not $env:PSYCOPG_PG_ROOT) { throw "PSYCOPG_PG_ROOT missing from .env" }
    $env:PSYCOPG_IMPL = "qt-thread"
    $env:QPSYCOPG_WORKER_IMPL = "c"

    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    $folder = Join-Path $repoRoot "test-results\qt-thread-queue-wait-ab-$timestamp"
    $archive = Join-Path $repoRoot "test-results\qt-thread-queue-wait-ab-$timestamp.zip"
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    $resultFile = Join-Path $folder "results.json"
    $stderrFile = Join-Path $folder "stderr.log"

    [ordered]@{
        commit = (& git -C $repoRoot rev-parse HEAD).Trim()
        branch = (& git -C $repoRoot branch --show-current).Trim()
        diagnostic = "timed-versus-untimed SimpleQueue.get response wait"
        runtime_changed = $false
        benchmark_v3_changed = $false
        probe_sha256 = (Get-FileHash -Path $probe -Algorithm SHA256).Hash
        pairs = $Pairs
        iterations = $Iterations
        warmup = $Warmup
        watchdog_seconds = $WatchdogSeconds
        note = "Expected watchdog timeout for untimed worker-exit scenario is a safety failure of that candidate, not a runner failure."
    } | ConvertTo-Json | Set-Content -Path (Join-Path $folder "manifest.json") -Encoding utf8

    Write-Host "qt-thread queue wait A/B: $Pairs interleaved pairs, $Iterations requests/mode/pair"
    Write-Host "Worker failure scenarios run in child processes with $WatchdogSeconds-second watchdog"
    & $forkPython $bootstrap $probe $env:QPSYCOPG_DSN --pairs $Pairs --iterations $Iterations --warmup $Warmup --watchdog-seconds $WatchdogSeconds > $resultFile 2> $stderrFile
    $exitCode = $LASTEXITCODE

    Compress-Archive -Path (Join-Path $folder "*") -DestinationPath $archive -CompressionLevel Optimal -Force
    Write-Host "Diagnostic archive: $archive"
    if ($exitCode -ne 0) {
        Write-Warning "A/B runner returned $exitCode. Include the ZIP with your report; results/errors were preserved."
    }
} finally {
    if ($null -eq $originalImpl) {
        Remove-Item Env:PSYCOPG_IMPL -ErrorAction Ignore
    } else {
        $env:PSYCOPG_IMPL = $originalImpl
    }
    if ($null -eq $originalWorker) {
        Remove-Item Env:QPSYCOPG_WORKER_IMPL -ErrorAction Ignore
    } else {
        $env:QPSYCOPG_WORKER_IMPL = $originalWorker
    }
    foreach ($entry in $addedConfig.GetEnumerator()) {
        if ($null -eq $entry.Value) {
            Remove-Item "Env:$($entry.Key)" -ErrorAction Ignore
        } else {
            [Environment]::SetEnvironmentVariable($entry.Key, [string] $entry.Value)
        }
    }
}
