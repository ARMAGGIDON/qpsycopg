param(
    [int] $Repeats = 4,
    [int] $Cycles = 12,
    [int] $BlockSize = 1000,
    [int] $Warmup = 250,
    [int] $BlockWarmup = 32,
    [double] $WatchdogSeconds = 120
)

$ErrorActionPreference = "Stop"
if ($Repeats -lt 4 -or $Cycles -lt 8 -or $Cycles % 2 -ne 0 -or $BlockSize -lt 500 -or $Warmup -lt 0 -or $BlockWarmup -lt 0 -or $WatchdogSeconds -lt 20) {
    throw "Requires Repeats>=4, even Cycles>=8, BlockSize>=500, nonnegative warmups, watchdog>=20"
}

$repoRoot = Split-Path $PSScriptRoot -Parent
$bootstrapPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$forkPython = Join-Path $repoRoot "test-results\qpsycopg-benchmark\.venv-fork-bench\Scripts\python.exe"
$configScript = Join-Path $PSScriptRoot "qpsycopg_env.py"
$bootstrap = Join-Path $PSScriptRoot "run_with_pg_root.py"
$probe = Join-Path $PSScriptRoot "bench_qt_thread_native_result_boundary.py"
foreach ($path in @($bootstrapPython, $forkPython, $configScript, $bootstrap, $probe)) {
    if (-not (Test-Path $path)) { throw "Missing native result boundary prerequisite: $path" }
}

$originalImpl = $env:PSYCOPG_IMPL
$originalWorker = $env:QPSYCOPG_WORKER_IMPL
$addedConfig = @{}
try {
    $config = & $bootstrapPython $configScript --json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "Cannot read local qpsycopg .env" }
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
    $folder = Join-Path $repoRoot "test-results\qt-thread-native-boundary-ab-$timestamp"
    $archive = Join-Path $repoRoot "test-results\qt-thread-native-boundary-ab-$timestamp.zip"
    New-Item -ItemType Directory -Path $folder -Force | Out-Null
    $resultFile = Join-Path $folder "results.json"
    $stderrFile = Join-Path $folder "stderr.log"
    [ordered]@{
        commit = (& git -C $repoRoot rev-parse HEAD).Trim()
        branch = (& git -C $repoRoot branch --show-current).Trim()
        diagnostic = "warmed SELECT 1 native PGresult handoff versus current snapshot result boundary"
        probe_sha256 = (Get-FileHash -Path $probe -Algorithm SHA256).Hash
        repeats = $Repeats
        cycles_per_repeat = $Cycles
        block_size = $BlockSize
        warmup_per_mode = $Warmup
        block_warmup = $BlockWarmup
        watchdog_per_repeat_seconds = $WatchdogSeconds
        sql = "SELECT 1"
        execution = "same native PGconn.exec_params in one worker thread"
        adaptation = "same qt-thread cursor context, fresh Transformer per call"
        queue_handoffs = 2
        production_runtime_changed = $false
        benchmark_v3_changed = $false
        minimum_cpu_benefit_us_per_select = 15
    } | ConvertTo-Json | Set-Content -Path (Join-Path $folder "manifest.json") -Encoding utf8

    Write-Host "Native result boundary A/B only: $Repeats independent repeats x $Cycles five-block cycles"
    Write-Host "Same warmed SELECT 1 on one worker connection; two queue handoffs."
    $exitCode = 1
    try {
        & $forkPython $bootstrap $probe $env:QPSYCOPG_DSN --repeats $Repeats --cycles $Cycles --block-size $BlockSize --warmup $Warmup --block-warmup $BlockWarmup --watchdog-seconds $WatchdogSeconds > $resultFile 2> $stderrFile
        $exitCode = $LASTEXITCODE
    } catch {
        Add-Content -Path $stderrFile -Value ($_ | Out-String)
    }
    Compress-Archive -Path (Join-Path $folder "*") -DestinationPath $archive -CompressionLevel Optimal -Force
    Write-Host "Native boundary diagnostic archive: $archive"
    if ($exitCode -ne 0) {
        Write-Warning "Native boundary diagnostic exited $exitCode. Upload ZIP for error analysis."
    } else {
        Write-Host "Upload ZIP for paired CPU/wall/context-switch analysis; runtime change is not authorized."
    }
} finally {
    if ($null -eq $originalImpl) { Remove-Item Env:PSYCOPG_IMPL -ErrorAction Ignore }
    else { $env:PSYCOPG_IMPL = $originalImpl }
    if ($null -eq $originalWorker) { Remove-Item Env:QPSYCOPG_WORKER_IMPL -ErrorAction Ignore }
    else { $env:QPSYCOPG_WORKER_IMPL = $originalWorker }
    foreach ($entry in $addedConfig.GetEnumerator()) {
        if ($null -eq $entry.Value) { Remove-Item "Env:$($entry.Key)" -ErrorAction Ignore }
        else { [Environment]::SetEnvironmentVariable($entry.Key, [string] $entry.Value) }
    }
}
