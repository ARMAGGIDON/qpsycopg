param(
    [int] $Iterations = 3000,
    [int] $Rounds = 2
)

$ErrorActionPreference = "Stop"
if ($Iterations -le 0 -or $Rounds -le 0) { throw "Iterations and Rounds must be positive" }

$repoRoot = Split-Path $PSScriptRoot -Parent
$bootstrapPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$benchmarkRoot = Join-Path $repoRoot "test-results\qpsycopg-benchmark"
$upstreamPython = Join-Path $benchmarkRoot "upstream-99356800e90a\.venv-bench\Scripts\python.exe"
$forkPython = Join-Path $benchmarkRoot ".venv-fork-bench\Scripts\python.exe"
$bootstrap = Join-Path $PSScriptRoot "qpsycopg_env.py"
$runner = Join-Path $PSScriptRoot "run_with_pg_root.py"
$probe = Join-Path $PSScriptRoot "profile_qt_thread_short_query.py"

foreach ($path in @($bootstrapPython, $upstreamPython, $forkPython, $runner, $probe)) {
    if (-not (Test-Path $path)) {
        throw "Missing profiling prerequisite: $path. Keep the isolated benchmark v3 environments."
    }
}

$oldImpl = $env:PSYCOPG_IMPL
$oldWorker = $env:QPSYCOPG_WORKER_IMPL
$bootstrapEnv = @{}

try {
    $config = & $bootstrapPython $bootstrap --json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "Failed to read local benchmark .env" }
    foreach ($entry in $config.PSObject.Properties) {
        $old = [Environment]::GetEnvironmentVariable($entry.Name)
        $bootstrapEnv[$entry.Name] = $old
        if ($null -eq $old) {
            [Environment]::SetEnvironmentVariable($entry.Name, [string] $entry.Value)
        }
    }
    $dsn = $env:QPSYCOPG_DSN
    if (-not $dsn) { throw "QPSYCOPG_DSN must be configured in .env" }
    if (-not $env:PSYCOPG_PG_ROOT) { throw "PSYCOPG_PG_ROOT must be configured in .env" }

    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    $outDir = Join-Path $repoRoot "test-results\qt-thread-cpu-profile-$timestamp"
    $archive = Join-Path $repoRoot "test-results\qt-thread-cpu-profile-$timestamp.zip"
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null
    [ordered]@{
        commit = (& git -C $repoRoot rev-parse HEAD).Trim()
        branch = (& git -C $repoRoot branch --show-current).Trim()
        upstream_base = "99356800e90aa0e7b604f652d714ef0f33422b47"
        diagnostic_only = $true
        iterations = $Iterations
        warmup = 200
        rounds = $Rounds
        note = "Do not use profiled wall/CPU for optimization acceptance; benchmark v3 remains unchanged."
    } | ConvertTo-Json | Set-Content (Join-Path $outDir "manifest.json") -Encoding utf8

    for ($i = 1; $i -le $Rounds; $i++) {
        $number = "{0:D2}" -f $i
        $order = if ($i % 2 -eq 1) {
            @("upstream", "thread", "direct")
        } else {
            @("direct", "thread", "upstream")
        }
        foreach ($mode in $order) {
            $py = if ($mode -eq "upstream") { $upstreamPython } else { $forkPython }
            $env:PSYCOPG_IMPL = if ($mode -eq "thread") { "qt-thread" } else { "c" }
            if ($mode -eq "thread") {
                $env:QPSYCOPG_WORKER_IMPL = "c"
            } else {
                Remove-Item Env:QPSYCOPG_WORKER_IMPL -ErrorAction Ignore
            }
            Write-Host "[$mode] CPU profile $i/$Rounds"
            $file = Join-Path $outDir "profile-$number-$mode.json"
            & $py $runner $probe $dsn --iterations $Iterations --warmup 200 > $file
            if ($LASTEXITCODE -ne 0) { throw "$mode profile $i failed: $LASTEXITCODE" }
        }
    }

    Compress-Archive -Path (Join-Path $outDir "*") -DestinationPath $archive -CompressionLevel Optimal -Force
    Write-Host "CPU attribution archive: $archive"
} finally {
    if ($null -eq $oldImpl) { Remove-Item Env:PSYCOPG_IMPL -ErrorAction Ignore } else { $env:PSYCOPG_IMPL = $oldImpl }
    if ($null -eq $oldWorker) { Remove-Item Env:QPSYCOPG_WORKER_IMPL -ErrorAction Ignore } else { $env:QPSYCOPG_WORKER_IMPL = $oldWorker }
    foreach ($entry in $bootstrapEnv.GetEnumerator()) {
        if ($null -eq $entry.Value) {
            Remove-Item "Env:$($entry.Key)" -ErrorAction Ignore
        } else {
            [Environment]::SetEnvironmentVariable($entry.Key, [string] $entry.Value)
        }
    }
}
