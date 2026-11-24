$ErrorActionPreference = "Stop"
$repoRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$dsn = $env:QPSYCOPG_DSN
if (-not (Test-Path $python)) { throw "Virtual environment not found: $python" }
if (-not $dsn) { throw "QPSYCOPG_DSN is not set" }

$uv = Get-Command uv -ErrorAction Stop
Write-Host "[build] psycopg-c native IPC"
& $uv.Source pip install --python $python --reinstall --no-deps -e (Join-Path $repoRoot "psycopg_c")
if ($LASTEXITCODE -ne 0) { throw "psycopg-c rebuild failed with code $LASTEXITCODE" }

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$outDir = Join-Path $repoRoot "test-results\worker-hot-path-$timestamp"
$archive = Join-Path $repoRoot "test-results\qpsycopg-worker-hot-path-$timestamp.zip"
New-Item -ItemType Directory -Path $outDir -Force | Out-Null

$oldImpl = $env:PSYCOPG_IMPL
$oldWorker = $env:QPSYCOPG_WORKER_IMPL
$oldIpc = $env:QPSYCOPG_IPC
$oldNative = $env:QPSYCOPG_NATIVE_IPC
$oldProfile = $env:QPSYCOPG_PROFILE_PHASES

try {
    $env:PSYCOPG_IMPL = "qt-process"
    $env:QPSYCOPG_WORKER_IMPL = "binary"
    $env:QPSYCOPG_IPC = "pipe"
    $env:QPSYCOPG_NATIVE_IPC = "1"
    $env:QPSYCOPG_PROFILE_PHASES = "1"

    Write-Host "[profile] worker short-query CPU phases"
    & $python (Join-Path $PSScriptRoot "profile_worker_hot_path.py") $dsn > (Join-Path $outDir "worker-hot-path.json")
    if ($LASTEXITCODE -ne 0) { throw "worker hot-path profile failed with code $LASTEXITCODE" }

    [ordered]@{
        timestamp = (Get-Date).ToString("o")
        branch = (& git -C $repoRoot branch --show-current).Trim()
        commit = (& git -C $repoRoot rev-parse HEAD).Trim()
        iterations = 10000
        ipc = "pipe"
        native_ipc = $true
    } | ConvertTo-Json | Set-Content (Join-Path $outDir "manifest.json") -Encoding utf8

    Compress-Archive -Path (Join-Path $outDir "*") -DestinationPath $archive -CompressionLevel Optimal -Force
    Remove-Item $outDir -Recurse -Force
    Write-Host "Worker hot-path archive: $archive"
} finally {
    if ($null -eq $oldImpl) { Remove-Item Env:PSYCOPG_IMPL -ErrorAction Ignore } else { $env:PSYCOPG_IMPL = $oldImpl }
    if ($null -eq $oldWorker) { Remove-Item Env:QPSYCOPG_WORKER_IMPL -ErrorAction Ignore } else { $env:QPSYCOPG_WORKER_IMPL = $oldWorker }
    if ($null -eq $oldIpc) { Remove-Item Env:QPSYCOPG_IPC -ErrorAction Ignore } else { $env:QPSYCOPG_IPC = $oldIpc }
    if ($null -eq $oldNative) { Remove-Item Env:QPSYCOPG_NATIVE_IPC -ErrorAction Ignore } else { $env:QPSYCOPG_NATIVE_IPC = $oldNative }
    if ($null -eq $oldProfile) { Remove-Item Env:QPSYCOPG_PROFILE_PHASES -ErrorAction Ignore } else { $env:QPSYCOPG_PROFILE_PHASES = $oldProfile }
}
