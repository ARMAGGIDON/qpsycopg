$ErrorActionPreference = "Stop"
$repoRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$dsn = $env:QPSYCOPG_DSN
if (-not (Test-Path $python)) { throw "Virtual environment not found: $python" }
if (-not $dsn) { throw "QPSYCOPG_DSN is not set" }

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$outDir = Join-Path $repoRoot "test-results\cpu-$timestamp"
$archive = Join-Path $repoRoot "test-results\qpsycopg-cpu-$timestamp.zip"
New-Item -ItemType Directory -Path $outDir -Force | Out-Null

$oldImpl = $env:PSYCOPG_IMPL
$oldWorker = $env:QPSYCOPG_WORKER_IMPL
$oldIpc = $env:QPSYCOPG_IPC

try {
    $env:PSYCOPG_IMPL = "binary"
    Remove-Item Env:QPSYCOPG_WORKER_IMPL, Env:QPSYCOPG_IPC -ErrorAction Ignore
    & $python (Join-Path $PSScriptRoot "profile_cpu_path.py") $dsn > (Join-Path $outDir "cpu-vanilla.json")
    if ($LASTEXITCODE -ne 0) { throw "vanilla CPU profile failed with code $LASTEXITCODE" }

    $env:PSYCOPG_IMPL = "qt-process"
    $env:QPSYCOPG_WORKER_IMPL = "binary"
    $env:QPSYCOPG_IPC = "tcp"
    & $python (Join-Path $PSScriptRoot "profile_cpu_path.py") $dsn > (Join-Path $outDir "cpu-qpsycopg.json")
    if ($LASTEXITCODE -ne 0) { throw "qpsycopg CPU profile failed with code $LASTEXITCODE" }

    Compress-Archive -Path (Join-Path $outDir "*") -DestinationPath $archive -CompressionLevel Optimal -Force
    Remove-Item $outDir -Recurse -Force
    Write-Host "CPU profile archive: $archive"
} finally {
    if ($null -eq $oldImpl) { Remove-Item Env:PSYCOPG_IMPL -ErrorAction Ignore } else { $env:PSYCOPG_IMPL = $oldImpl }
    if ($null -eq $oldWorker) { Remove-Item Env:QPSYCOPG_WORKER_IMPL -ErrorAction Ignore } else { $env:QPSYCOPG_WORKER_IMPL = $oldWorker }
    if ($null -eq $oldIpc) { Remove-Item Env:QPSYCOPG_IPC -ErrorAction Ignore } else { $env:QPSYCOPG_IPC = $oldIpc }
}
