$ErrorActionPreference = "Stop"
$repoRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Virtual environment not found: $python" }

$uv = Get-Command uv -ErrorAction Stop
Write-Host "[build] psycopg-c native IPC helper"
& $uv.Source pip install --python $python --reinstall --no-deps -e (Join-Path $repoRoot "psycopg_c")
if ($LASTEXITCODE -ne 0) { throw "psycopg-c rebuild failed with code $LASTEXITCODE" }

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$outDir = Join-Path $repoRoot "test-results\native-ipc-$timestamp"
$archive = Join-Path $repoRoot "test-results\qpsycopg-native-ipc-$timestamp.zip"
New-Item -ItemType Directory -Path $outDir -Force | Out-Null

Write-Host "[bench] Python FileIO vs native ReadFile/WriteFile"
& $python (Join-Path $PSScriptRoot "bench_native_ipc.py") > (Join-Path $outDir "native-ipc.json")
if ($LASTEXITCODE -ne 0) { throw "native IPC benchmark failed with code $LASTEXITCODE" }

[ordered]@{
    timestamp = (Get-Date).ToString("o")
    branch = (& git -C $repoRoot branch --show-current).Trim()
    commit = (& git -C $repoRoot rev-parse HEAD).Trim()
} | ConvertTo-Json | Set-Content (Join-Path $outDir "manifest.json") -Encoding utf8

Compress-Archive -Path (Join-Path $outDir "*") -DestinationPath $archive -CompressionLevel Optimal -Force
Remove-Item $outDir -Recurse -Force
Write-Host "Native IPC archive: $archive"
