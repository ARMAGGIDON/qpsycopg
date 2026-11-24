param(
    [int] $Pairs = 8,
    [int] $Iterations = 4000,
    [int] $Warmup = 300,
    [double] $WatchdogSeconds = 90
)

$ErrorActionPreference = "Stop"
if ($Pairs -lt 4 -or $Iterations -lt 100 -or $Warmup -lt 0 -or $WatchdogSeconds -lt 10) {
    throw "Expected Pairs>=4, Iterations>=100, Warmup>=0, WatchdogSeconds>=10"
}

$repoRoot = Split-Path $PSScriptRoot -Parent
$bootstrapPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$forkPython = Join-Path $repoRoot "test-results\qpsycopg-benchmark\.venv-fork-bench\Scripts\python.exe"
$configScript = Join-Path $PSScriptRoot "qpsycopg_env.py"
$bootstrap = Join-Path $PSScriptRoot "run_with_pg_root.py"
$probe = Join-Path $PSScriptRoot "bench_qt_thread_echo_baseline.py"
foreach ($path in @($bootstrapPython, $forkPython, $configScript, $bootstrap, $probe)) {
    if (-not (Test-Path $path)) {
        throw "Missing echo diagnostic prerequisite: $path"
    }
}

$originalImpl = $env:PSYCOPG_IMPL
$originalWorker = $env:QPSYCOPG_WORKER_IMPL
$addedConfig = @{}
try {
    $config = & $bootstrapPython $configScript --json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "Cannot read qpsycopg local .env" }
    foreach ($entry in $config.PSObject.Properties) {
        $previous = [Environment]::GetEnvironmentVariable($entry.Name)
        $addedConfig[$entry.Name] = $previous
        if ($null -eq $previous) {
            [Environment]::SetEnvironmentVariable($entry.Name, [string] $entry.Value)
        }
    }
    if (-not $env:QPSYCOPG_DSN) { throw "QPSYCOPG_DSN missing from local .env" }
    if (-not $env:PSYCOPG_PG_ROOT) { throw "PSYCOPG_PG_ROOT missing from local .env" }

    $env:PSYCOPG_IMPL = "qt-thread"
    $env:QPSYCOPG_WORKER_IMPL = "c"
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    $folder = Join-Path $repoRoot "test-results\qt-thread-echo-baseline-$timestamp"
    $archive = Join-Path $repoRoot "test-results\qt-thread-echo-baseline-$timestamp.zip"
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    $resultFile = Join-Path $folder "results.json"
    $stderrFile = Join-Path $folder "stderr.log"

    [ordered]@{
        commit = (& git -C $repoRoot rev-parse HEAD).Trim()
        branch = (& git -C $repoRoot branch --show-current).Trim()
        diagnostic = "minimal SimpleQueue request-response echo versus qpsycopg GET status"
        runtime_changed = $false
        benchmark_v3_changed = $false
        probe_sha256 = (Get-FileHash -Path $probe -Algorithm SHA256).Hash
        pairs = $Pairs
        iterations = $Iterations
        warmup = $Warmup
        watchdog_seconds = $WatchdogSeconds
        impl = "qt-thread"
        worker_impl = "c"
    } | ConvertTo-Json | Set-Content -Path (Join-Path $folder "manifest.json") -Encoding utf8

    Write-Host "SimpleQueue echo vs qt-thread GET status: $Pairs alternating pairs, $Iterations calls/mode/pair"
    $exitCode = 1
    try {
        & $forkPython $bootstrap $probe $env:QPSYCOPG_DSN --pairs $Pairs --iterations $Iterations --warmup $Warmup --watchdog-seconds $WatchdogSeconds > $resultFile 2> $stderrFile
        $exitCode = $LASTEXITCODE
    } catch {
        Add-Content -Path $stderrFile -Value ($_ | Out-String)
    }

    Compress-Archive -Path (Join-Path $folder "*") -DestinationPath $archive -CompressionLevel Optimal -Force
    Write-Host "Echo baseline diagnostic archive: $archive"
    if ($exitCode -ne 0) {
        Write-Warning "Echo diagnostic exited with code $exitCode; archive includes the failure details."
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
