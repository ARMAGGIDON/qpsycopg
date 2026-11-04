param(
    [int] $Repeats = 4,
    [int] $Cycles = 16,
    [int] $BlockSize = 600,
    [int] $Warmup = 250,
    [double] $WatchdogSeconds = 90
)

$ErrorActionPreference = "Stop"
if ($Repeats -lt 3 -or $Cycles -lt 8 -or $BlockSize -lt 200 -or $Warmup -lt 0 -or $WatchdogSeconds -lt 10) {
    throw "Expected Repeats>=3, Cycles>=8, BlockSize>=200, Warmup>=0, WatchdogSeconds>=10"
}

$repoRoot = Split-Path $PSScriptRoot -Parent
$bootstrapPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$forkPython = Join-Path $repoRoot "test-results\qpsycopg-benchmark\.venv-fork-bench\Scripts\python.exe"
$configScript = Join-Path $PSScriptRoot "qpsycopg_env.py"
$bootstrap = Join-Path $PSScriptRoot "run_with_pg_root.py"
$probe = Join-Path $PSScriptRoot "bench_qt_thread_enriched_echo.py"
foreach ($path in @($bootstrapPython, $forkPython, $configScript, $bootstrap, $probe)) {
    if (-not (Test-Path $path)) {
        throw "Missing enriched echo prerequisite: $path. Keep the existing v3 fork venv."
    }
}

$originalImpl = $env:PSYCOPG_IMPL
$originalWorker = $env:QPSYCOPG_WORKER_IMPL
$addedConfig = @{}
try {
    $config = & $bootstrapPython $configScript --json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "Cannot load local qpsycopg .env" }
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
    $folder = Join-Path $repoRoot "test-results\qt-thread-enriched-echo-$timestamp"
    $archive = Join-Path $repoRoot "test-results\qt-thread-enriched-echo-$timestamp.zip"
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    $resultFile = Join-Path $folder "results.json"
    $stderrFile = Join-Path $folder "stderr.log"
    [ordered]@{
        commit = (& git -C $repoRoot rev-parse HEAD).Trim()
        branch = (& git -C $repoRoot branch --show-current).Trim()
        diagnostic = "short bracketed, balanced enriched echo vs real qt-thread status GET"
        production_runtime_changed = $false
        benchmark_v3_changed = $false
        probe_sha256 = (Get-FileHash -Path $probe -Algorithm SHA256).Hash
        repeats = $Repeats
        cycles_per_repeat = $Cycles
        calls_per_block = $BlockSize
        warmup_per_mode = $Warmup
        watchdog_per_repeat_seconds = $WatchdogSeconds
        implementation = "qt-thread"
        worker_impl = "c"
    } | ConvertTo-Json | Set-Content -Path (Join-Path $folder "manifest.json") -Encoding utf8

    Write-Host "Enriched echo: $Repeats independent child repetitions x $Cycles short bracketed cycles"
    Write-Host "Each cycle: bare echo - four balanced variants - bare echo"
    $exitCode = 1
    try {
        & $forkPython $bootstrap $probe $env:QPSYCOPG_DSN --repeats $Repeats --cycles $Cycles --block-size $BlockSize --warmup $Warmup --watchdog-seconds $WatchdogSeconds > $resultFile 2> $stderrFile
        $exitCode = $LASTEXITCODE
    } catch {
        Add-Content -Path $stderrFile -Value ($_ | Out-String)
    }
    Compress-Archive -Path (Join-Path $folder "*") -DestinationPath $archive -CompressionLevel Optimal -Force
    Write-Host "Diagnostic archive: $archive"
    if ($exitCode -ne 0) {
        Write-Warning "Enriched echo diagnostic returned $exitCode. Preserve and share ZIP for error analysis."
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
