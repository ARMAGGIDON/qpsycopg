param(
    [double] $WatchdogSeconds = 40
)

$ErrorActionPreference = "Stop"
if ($WatchdogSeconds -lt 10) { throw "WatchdogSeconds must be >=10" }

$repoRoot = Split-Path $PSScriptRoot -Parent
$bootstrapPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$forkPython = Join-Path $repoRoot "test-results\qpsycopg-benchmark\.venv-fork-bench\Scripts\python.exe"
$configScript = Join-Path $PSScriptRoot "qpsycopg_env.py"
$bootstrap = Join-Path $PSScriptRoot "run_with_pg_root.py"
$probe = Join-Path $PSScriptRoot "preflight_qt_thread_native_pgresult.py"
foreach ($file in @($bootstrapPython, $forkPython, $configScript, $bootstrap, $probe)) {
    if (-not (Test-Path $file)) { throw "Missing native-result preflight prerequisite: $file" }
}

$beforeImpl = $env:PSYCOPG_IMPL
$beforeWorker = $env:QPSYCOPG_WORKER_IMPL
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
    $folder = Join-Path $repoRoot "test-results\qt-thread-native-result-preflight-$timestamp"
    $archive = Join-Path $repoRoot "test-results\qt-thread-native-result-preflight-$timestamp.zip"
    New-Item -ItemType Directory -Path $folder -Force | Out-Null
    $resultFile = Join-Path $folder "results.json"
    $stderrFile = Join-Path $folder "stderr.log"
    [ordered]@{
        commit = (& git -C $repoRoot rev-parse HEAD).Trim()
        branch = (& git -C $repoRoot branch --show-current).Trim()
        diagnostic = "native C PGresult ownership and Transformer two-queue feasibility preflight"
        probe_sha256 = (Get-FileHash -Path $probe -Algorithm SHA256).Hash
        production_runtime_changed = $false
        benchmark_v3_changed = $false
        watchdog_seconds = $WatchdogSeconds
        pq_impl = "qt-thread"
        worker_impl = "c"
        gate = "STOP if native pointer, lifetime, notice, metadata, NULL/error or native Transformer parity fails"
    } | ConvertTo-Json | Set-Content -Path (Join-Path $folder "manifest.json") -Encoding utf8

    Write-Host "Native PGresult preflight only; performance A/B is not yet authorised"
    $exitCode = 1
    try {
        & $forkPython $bootstrap $probe $env:QPSYCOPG_DSN --watchdog-seconds $WatchdogSeconds > $resultFile 2> $stderrFile
        $exitCode = $LASTEXITCODE
    } catch {
        Add-Content -Path $stderrFile -Value ($_ | Out-String)
    }

    Compress-Archive -Path (Join-Path $folder "*") -DestinationPath $archive -Force -CompressionLevel Optimal
    Write-Host "Native PGresult preflight archive: $archive"
    if ($exitCode -ne 0) {
        Write-Warning "Native preflight failed with exit $exitCode. Upload ZIP; do NOT run performance A/B."
    } else {
        Write-Host "Upload ZIP for independent feasibility review BEFORE any A/B."
    }
} finally {
    if ($null -eq $beforeImpl) { Remove-Item Env:PSYCOPG_IMPL -ErrorAction Ignore }
    else { $env:PSYCOPG_IMPL = $beforeImpl }
    if ($null -eq $beforeWorker) { Remove-Item Env:QPSYCOPG_WORKER_IMPL -ErrorAction Ignore }
    else { $env:QPSYCOPG_WORKER_IMPL = $beforeWorker }
    foreach ($entry in $addedConfig.GetEnumerator()) {
        if ($null -eq $entry.Value) { Remove-Item "Env:$($entry.Key)" -ErrorAction Ignore }
        else { [Environment]::SetEnvironmentVariable($entry.Key, [string] $entry.Value) }
    }
}
