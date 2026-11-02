$ErrorActionPreference = "Stop"
$repoRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$dsn = $env:QPSYCOPG_DSN
if (-not (Test-Path $python)) { throw "Virtual environment not found: $python" }
if (-not $dsn) { throw "QPSYCOPG_DSN is not set" }

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$outDir = Join-Path $repoRoot "test-results\transport-validation-$timestamp"
$archive = Join-Path $repoRoot "test-results\qpsycopg-transport-validation-$timestamp.zip"
New-Item -ItemType Directory -Path $outDir -Force | Out-Null

$oldImpl = $env:PSYCOPG_IMPL
$oldWorker = $env:QPSYCOPG_WORKER_IMPL
$oldIpc = $env:QPSYCOPG_IPC
$bench = Join-Path $PSScriptRoot "bench_qt_process.py"
$qt = Join-Path $PSScriptRoot "bench_qt_responsiveness.py"
$cpu = Join-Path $PSScriptRoot "profile_cpu_phases.py"
$smoke = Join-Path $PSScriptRoot "smoke_qt_process.py"

function Set-Qpsycopg([string] $ipc) {
    $env:PSYCOPG_IMPL = "qt-process"
    $env:QPSYCOPG_WORKER_IMPL = "binary"
    $env:QPSYCOPG_IPC = $ipc
}

function Run([string] $ipc, [string] $label, [string[]] $pythonArgs, [string] $file) {
    Write-Host "[$ipc] $label"
    Set-Qpsycopg $ipc
    & $python @pythonArgs > (Join-Path $outDir $file)
    if ($LASTEXITCODE -ne 0) { throw "$ipc $label failed with code $LASTEXITCODE" }
}

try {
    Set-Qpsycopg "pipe"
    & $python $smoke $dsn *> (Join-Path $outDir "smoke-pipe.log")
    if ($LASTEXITCODE -ne 0) { throw "pipe smoke failed with code $LASTEXITCODE" }

    $benchArgs = @($bench, $dsn, "--iterations", "500", "--rows", "100000", "--threads", "8")
    $qtArgs = @($qt, $dsn, "--caller", "thread")

    for ($i = 1; $i -le 5; $i++) {
        $n = "{0:D2}" -f $i
        $order = if ($i % 2) { @("tcp", "pipe") } else { @("pipe", "tcp") }
        foreach ($ipc in $order) {
            Run $ipc "SQL $i/5" $benchArgs "bench-$n-$ipc.json"
            Run $ipc "Qt $i/5" $qtArgs "qt-$n-$ipc.json"
        }
    }

    Run "tcp" "CPU phases" @($cpu, $dsn) "cpu-phases-tcp.json"
    Run "pipe" "CPU phases" @($cpu, $dsn) "cpu-phases-pipe.json"

    [ordered]@{
        timestamp = (Get-Date).ToString("o")
        branch = (& git -C $repoRoot branch --show-current).Trim()
        commit = (& git -C $repoRoot rev-parse HEAD).Trim()
        repeats = 5
        iterations = 500
        rows = 100000
        threads = 8
    } | ConvertTo-Json | Set-Content (Join-Path $outDir "manifest.json") -Encoding utf8

    Compress-Archive -Path (Join-Path $outDir "*") -DestinationPath $archive -CompressionLevel Optimal -Force
    Remove-Item $outDir -Recurse -Force
    Write-Host "Transport validation archive: $archive"
} finally {
    if ($null -eq $oldImpl) { Remove-Item Env:PSYCOPG_IMPL -ErrorAction Ignore } else { $env:PSYCOPG_IMPL = $oldImpl }
    if ($null -eq $oldWorker) { Remove-Item Env:QPSYCOPG_WORKER_IMPL -ErrorAction Ignore } else { $env:QPSYCOPG_WORKER_IMPL = $oldWorker }
    if ($null -eq $oldIpc) { Remove-Item Env:QPSYCOPG_IPC -ErrorAction Ignore } else { $env:QPSYCOPG_IPC = $oldIpc }
}
