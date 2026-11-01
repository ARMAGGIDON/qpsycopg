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
    [string] $Tag = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Virtual environment not found: $python" }
if (-not $Dsn) { $Dsn = "postgresql://postgres:postgres@127.0.0.1:5432/postgres" }

$oldImpl = $env:PSYCOPG_IMPL
$oldWorker = $env:QPSYCOPG_WORKER_IMPL

function Set-Backend([string] $impl, [string] $worker = "") {
    $env:PSYCOPG_IMPL = $impl
    if ($worker) { $env:QPSYCOPG_WORKER_IMPL = $worker }
    else { Remove-Item Env:QPSYCOPG_WORKER_IMPL -ErrorAction Ignore }
}

function Invoke-Python([string[]] $arguments, [string] $outFile = "") {
    if ($outFile) {
        & $python @arguments | Tee-Object -FilePath (Join-Path $repoRoot $outFile)
    } else {
        & $python @arguments
    }
    if ($LASTEXITCODE -ne 0) { throw "Python exited with code $LASTEXITCODE" }
}

function Name([string] $base) {
    if ($Tag) { return "$base-$Tag.json" }
    return "$base.json"
}

function Smoke {
    Set-Backend "qt-process" $Worker
    Invoke-Python @((Join-Path $repoRoot "tools\smoke_qt_process.py"), $Dsn)
}

function Bench {
    $args = @(
        (Join-Path $repoRoot "tools\bench_qt_process.py"), $Dsn,
        "--iterations", $Iterations, "--rows", $Rows, "--threads", $Threads
    )

    Set-Backend $Worker
    Invoke-Python $args (Name "vanilla")

    Set-Backend "qt-process" $Worker
    Invoke-Python $args (Name "qpsycopg")
}

function QtBench {
    $script = Join-Path $repoRoot "tools\bench_qt_responsiveness.py"

    Set-Backend $Worker
    Invoke-Python @($script, $Dsn, "--caller", "thread") (Name "vanilla-qt")

    Set-Backend "qt-process" $Worker
    Invoke-Python @($script, $Dsn, "--caller", "thread") (Name "qpsycopg-qt")
}

function GuiBlock {
    Set-Backend "qt-process" $Worker
    Invoke-Python @((Join-Path $repoRoot "tools\bench_qt_responsiveness.py"), $Dsn, "--caller", "main") (Name "guiBlocks")
}

try {
    switch ($Mode) {
        "smoke" { Smoke }
        "bench" { Bench }
        "qt" { QtBench }
        "gui" { GuiBlock }
        "all" { Smoke; Bench; QtBench }
    }
} finally {
    if ($null -eq $oldImpl) { Remove-Item Env:PSYCOPG_IMPL -ErrorAction Ignore }
    else { $env:PSYCOPG_IMPL = $oldImpl }

    if ($null -eq $oldWorker) { Remove-Item Env:QPSYCOPG_WORKER_IMPL -ErrorAction Ignore }
    else { $env:QPSYCOPG_WORKER_IMPL = $oldWorker }
}
