param(
    [Parameter(Position = 0)]
    [ValidateSet("smoke", "bench", "qt", "gui", "all")]
    [string] $Mode = "smoke",

    [string] $Dsn = $env:QPSYCOPG_DSN,
    [ValidateSet("binary", "python", "c")]
    [string] $Worker = "binary",
    [int] $Iterations = 500,
    [int] $Rows = 100000,
    [int] $Threads = 8,
    [string] $Tag = "",
    [switch] $KeepResults
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Virtual environment not found: $python" }
if (-not $Dsn) { $Dsn = "postgresql://postgres:postgres@127.0.0.1:5432/postgres" }

$safeTag = $Tag -replace '[^\w.-]', '_'
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$runId = if ($safeTag) { "$timestamp-$safeTag" } else { $timestamp }
$resultsRoot = Join-Path $repoRoot "test-results"
$runDir = Join-Path $resultsRoot $runId
$archive = Join-Path $resultsRoot "qpsycopg-$Mode-$runId.zip"
New-Item -ItemType Directory -Path $runDir -Force | Out-Null

$oldImpl = $env:PSYCOPG_IMPL
$oldWorker = $env:QPSYCOPG_WORKER_IMPL

function Set-Backend([string] $impl, [string] $worker = "") {
    $env:PSYCOPG_IMPL = $impl
    if ($worker) { $env:QPSYCOPG_WORKER_IMPL = $worker }
    else { Remove-Item Env:QPSYCOPG_WORKER_IMPL -ErrorAction Ignore }
}

function Invoke-Python([string[]] $arguments, [string] $outFile = "") {
    if ($outFile) { & $python @arguments | Tee-Object -FilePath $outFile }
    else { & $python @arguments }
    if ($LASTEXITCODE -ne 0) { throw "Python exited with code $LASTEXITCODE" }
}

function Result([string] $name, [string] $extension = "json") {
    return Join-Path $runDir "$name.$extension"
}

function Smoke {
    Set-Backend "qt-process" $Worker
    Invoke-Python @((Join-Path $repoRoot "tools\smoke_qt_process.py"), $Dsn) (Result "smoke" "log")
}

function Bench {
    $args = @(
        (Join-Path $repoRoot "tools\bench_qt_process.py"), $Dsn,
        "--iterations", $Iterations, "--rows", $Rows, "--threads", $Threads
    )

    Set-Backend $Worker
    Invoke-Python $args (Result "vanilla")

    Set-Backend "qt-process" $Worker
    Invoke-Python $args (Result "qpsycopg")
}

function QtBench {
    $script = Join-Path $repoRoot "tools\bench_qt_responsiveness.py"

    Set-Backend $Worker
    Invoke-Python @($script, $Dsn, "--caller", "thread") (Result "vanilla-qt")

    Set-Backend "qt-process" $Worker
    Invoke-Python @($script, $Dsn, "--caller", "thread") (Result "qpsycopg-qt")
}

function GuiBlock {
    Set-Backend "qt-process" $Worker
    Invoke-Python @((Join-Path $repoRoot "tools\bench_qt_responsiveness.py"), $Dsn, "--caller", "main") (Result "guiBlocks")
}

function Save-Results {
    $manifest = [ordered]@{
        timestamp = (Get-Date).ToString("o")
        mode = $Mode
        tag = $Tag
        worker = $Worker
        iterations = $Iterations
        rows = $Rows
        threads = $Threads
        branch = (& git -C $repoRoot branch --show-current).Trim()
        commit = (& git -C $repoRoot rev-parse HEAD).Trim()
    }
    $manifest | ConvertTo-Json | Set-Content (Result "manifest") -Encoding utf8
    Compress-Archive -Path (Join-Path $runDir "*") -DestinationPath $archive -CompressionLevel Optimal -Force
    if (-not $KeepResults) { Remove-Item $runDir -Recurse -Force }
    Write-Host "Results archive: $archive"
}

try {
    switch ($Mode) {
        "smoke" { Smoke }
        "bench" { Bench }
        "qt" { QtBench }
        "gui" { GuiBlock }
        "all" { Smoke; Bench; QtBench }
    }
    Save-Results
} finally {
    if ($null -eq $oldImpl) { Remove-Item Env:PSYCOPG_IMPL -ErrorAction Ignore }
    else { $env:PSYCOPG_IMPL = $oldImpl }

    if ($null -eq $oldWorker) { Remove-Item Env:QPSYCOPG_WORKER_IMPL -ErrorAction Ignore }
    else { $env:QPSYCOPG_WORKER_IMPL = $oldWorker }
}
