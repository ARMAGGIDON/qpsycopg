param(
    [int] $Repeats = 4,
    [int] $Cycles = 12,
    [int] $BlockSize = 1200,
    [int] $Warmup = 250,
    [int] $BlockWarmup = 32,
    [double] $WatchdogSeconds = 90
)

$ErrorActionPreference = "Stop"
if ($Repeats -lt 3 -or $Cycles -lt 8 -or $BlockSize -lt 200 -or $Warmup -lt 0 -or $BlockWarmup -lt 0 -or $WatchdogSeconds -lt 10) {
    throw "Expected Repeats>=3, Cycles>=8, BlockSize>=200, nonnegative warmups and WatchdogSeconds>=10"
}

$repoRoot = Split-Path $PSScriptRoot -Parent
$bootstrapPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$forkPython = Join-Path $repoRoot "test-results\qpsycopg-benchmark\.venv-fork-bench\Scripts\python.exe"
$configScript = Join-Path $PSScriptRoot "qpsycopg_env.py"
$bootstrap = Join-Path $PSScriptRoot "run_with_pg_root.py"
$probe = Join-Path $PSScriptRoot "profile_qt_thread_get_real_path.py"
foreach ($path in @($bootstrapPython, $forkPython, $configScript, $bootstrap, $probe)) {
    if (-not (Test-Path $path)) {
        throw "Missing real-path profiling prerequisite: $path"
    }
}

$originalImpl = $env:PSYCOPG_IMPL
$originalWorker = $env:QPSYCOPG_WORKER_IMPL
$addedConfig = @{}
try {
    $config = & $bootstrapPython $configScript --json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "Cannot load qpsycopg local .env" }
    foreach ($entry in $config.PSObject.Properties) {
        $previous = [Environment]::GetEnvironmentVariable($entry.Name)
        $addedConfig[$entry.Name] = $previous
        if ($null -eq $previous) {
            [Environment]::SetEnvironmentVariable($entry.Name, [string] $entry.Value)
        }
    }

    if (-not $env:QPSYCOPG_DSN) { throw "QPSYCOPG_DSN missing in local .env" }
    if (-not $env:PSYCOPG_PG_ROOT) { throw "PSYCOPG_PG_ROOT missing in local .env" }
    $env:PSYCOPG_IMPL = "qt-thread"
    $env:QPSYCOPG_WORKER_IMPL = "c"

    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    $folder = Join-Path $repoRoot "test-results\qt-thread-real-get-profile-$timestamp"
    $archive = Join-Path $repoRoot "test-results\qt-thread-real-get-profile-$timestamp.zip"
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    $resultFile = Join-Path $folder "results.json"
    $stderrFile = Join-Path $folder "stderr.log"

    [ordered]@{
        commit = (& git -C $repoRoot rev-parse HEAD).Trim()
        branch = (& git -C $repoRoot branch --show-current).Trim()
        diagnostic = "real GET status request, perform, dispatch, response and worker _run/_handle inclusive timers"
        production_runtime_changed = $false
        benchmark_v3_changed = $false
        probe_sha256 = (Get-FileHash -Path $probe -Algorithm SHA256).Hash
        repeats = $Repeats
        cycles_per_repeat = $Cycles
        block_size = $BlockSize
        warmup_per_connection = $Warmup
        warmup_per_block = $BlockWarmup
        watchdog_per_repeat_seconds = $WatchdogSeconds
        implementation = "qt-thread"
        worker_impl = "c"
        note = "Control connection is uninstrumented. Profiled connection uses transparent worker queue observers and temporary local method hooks. Profiled timings overlap and include measurement overhead."
    } | ConvertTo-Json | Set-Content -Path (Join-Path $folder "manifest.json") -Encoding utf8

    Write-Host "Real qt-thread GET status: $Repeats child repeats x $Cycles short C-P-C-P-C cycles"
    Write-Host "Independent controls have original methods and raw worker queues."
    $exitCode = 1
    try {
        & $forkPython $bootstrap $probe $env:QPSYCOPG_DSN --repeats $Repeats --cycles $Cycles --block-size $BlockSize --warmup $Warmup --block-warmup $BlockWarmup --watchdog-seconds $WatchdogSeconds > $resultFile 2> $stderrFile
        $exitCode = $LASTEXITCODE
    } catch {
        Add-Content -Path $stderrFile -Value ($_ | Out-String)
    }

    Compress-Archive -Path (Join-Path $folder "*") -DestinationPath $archive -CompressionLevel Optimal -Force
    Write-Host "Real GET profiling archive: $archive"
    if ($exitCode -ne 0) {
        Write-Warning "Real-path diagnostic exited $exitCode. Share ZIP to inspect recorded error."
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
