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
    [ValidateRange(1, 20)]
    [int] $Repeat = 7,
    [string] $Tag = "",
    [switch] $KeepResults
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Virtual environment not found: $python" }
if (-not $Dsn) { throw "QPSYCOPG_DSN is not set" }

$resultsRoot = Join-Path $repoRoot "test-results"
New-Item -ItemType Directory -Path $resultsRoot -Force | Out-Null
if (-not $Tag -and $Mode -in @("bench", "all")) {
    $lastOptimized = Get-ChildItem $resultsRoot -File -Filter "qpsycopg-*.zip" |
        ForEach-Object { if ($_.BaseName -match '-optimized(\d+)$') { [int] $Matches[1] } } |
        Sort-Object -Descending | Select-Object -First 1
    $nextOptimized = if ($null -eq $lastOptimized) { 17 } else { [math]::Max(17, $lastOptimized + 1) }
    $Tag = "optimized$nextOptimized"
}

$safeTag = $Tag -replace '[^\w.-]', '_'
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$runId = if ($safeTag) { "$timestamp-$safeTag" } else { $timestamp }
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
    if ($outFile) { & $python @arguments > $outFile }
    else { & $python @arguments }
    if ($LASTEXITCODE -ne 0) { throw "Python exited with code $LASTEXITCODE" }
}

function Result([string] $name, [string] $extension = "json") {
    return Join-Path $runDir "$name.$extension"
}

function Invoke-BackendTest(
    [string] $label,
    [string] $impl,
    [string[]] $arguments,
    [string] $outFile
) {
    Write-Host $label
    if ($impl -eq "qt-process") { Set-Backend $impl $Worker }
    else { Set-Backend $impl }
    Invoke-Python $arguments $outFile
}

function Smoke {
    Write-Host "[smoke] qt-process/$Worker"
    Set-Backend "qt-process" $Worker
    $outFile = Result "smoke" "log"
    & $python (Join-Path $repoRoot "tools\smoke_qt_process.py") $Dsn 2>&1 | Tee-Object -FilePath $outFile
    if ($LASTEXITCODE -ne 0) { throw "Python exited with code $LASTEXITCODE" }
}

function Bench {
    $benchArgs = @(
        (Join-Path $repoRoot "tools\bench_qt_process.py"), $Dsn,
        "--iterations", $Iterations, "--rows", $Rows, "--threads", $Threads
    )

    for ($i = 1; $i -le $Repeat; $i++) {
        $n = "{0:D2}" -f $i
        if ($i % 2) {
            Invoke-BackendTest "[$i/$Repeat] vanilla bench" $Worker $benchArgs (Result "bench-$n-vanilla")
            Invoke-BackendTest "[$i/$Repeat] qpsycopg bench" "qt-process" $benchArgs (Result "bench-$n-qpsycopg")
        } else {
            Invoke-BackendTest "[$i/$Repeat] qpsycopg bench" "qt-process" $Worker $benchArgs (Result "bench-$n-qpsycopg")
            Invoke-BackendTest "[$i/$Repeat] vanilla bench" $Worker $benchArgs (Result "bench-$n-vanilla")
        }
    }
}

function QtBench {
    $qtArgs = @((Join-Path $repoRoot "tools\bench_qt_responsiveness.py"), $Dsn, "--caller", "thread")

    for ($i = 1; $i -le $Repeat; $i++) {
        $n = "{0:D2}" -f $i
        if ($i % 2) {
            Invoke-BackendTest "[$i/$Repeat] vanilla Qt" $Worker $qtArgs (Result "qt-$n-vanilla")
            Invoke-BackendTest "[$i/$Repeat] qpsycopg Qt" "qt-process" $Worker $qtArgs (Result "qt-$n-qpsycopg")
        } else {
            Invoke-BackendTest "[$i/$Repeat] qpsycopg Qt" "qt-process" $Worker $qtArgs (Result "qt-$n-qpsycopg")
            Invoke-BackendTest "[$i/$Repeat] vanilla Qt" $Worker $qtArgs (Result "qt-$n-vanilla")
        }
    }
}

function GuiBlock {
    Write-Host "[gui] main-thread blocking control"
    Set-Backend "qt-process" $Worker
    Invoke-Python @(
        (Join-Path $repoRoot "tools\bench_qt_responsiveness.py"), $Dsn, "--caller", "main"
    ) (Result "guiBlocks")
}

function Median([double[]] $values) {
    if (-not $values.Count) { return $null }
    $sorted = @($values | Sort-Object)
    $middle = [int] [math]::Floor($sorted.Count / 2)
    if ($sorted.Count % 2) { return [double] $sorted[$middle] }
    return ([double] $sorted[$middle - 1] + [double] $sorted[$middle]) / 2
}

function BenchSummary([string] $backend) {
    $runs = @(Get-ChildItem $runDir -Filter "bench-*-$backend.json" | Sort-Object Name |
        ForEach-Object { Get-Content $_.FullName -Raw | ConvertFrom-Json })
    if (-not $runs.Count) { return $null }

    return [ordered]@{
        runs = $runs.Count
        connect_median_ms = Median @($runs | ForEach-Object { $_.connect.median_ms })
        select_1_median_ms = Median @($runs | ForEach-Object { $_.select_1.median_ms })
        select_1_p95_ms = Median @($runs | ForEach-Object { $_.select_1.p95_ms })
        fetch_seconds = Median @($runs | ForEach-Object { $_.fetch.seconds })
        fetch_execute_seconds = Median @($runs | ForEach-Object { $_.fetch.execute_seconds })
        fetchall_seconds = Median @($runs | ForEach-Object { $_.fetch.fetchall_seconds })
        fetch_rows_per_second = Median @($runs | ForEach-Object { $_.fetch.rows_per_second })
        fetch_rss_before_mb = Median @($runs | ForEach-Object { $_.fetch.rss_before_mb })
        fetch_rss_after_mb = Median @($runs | ForEach-Object { $_.fetch.rss_after_mb })
        fetch_rss_delta_mb = Median @($runs | ForEach-Object { $_.fetch.rss_delta_mb })
        threads_seconds = Median @($runs | ForEach-Object { $_.threads.seconds })
        cancel_call_seconds = Median @($runs | ForEach-Object { $_.cancel.call_seconds })
    }
}

function QtSummary([string] $backend) {
    $runs = @(Get-ChildItem $runDir -Filter "qt-*-$backend.json" | Sort-Object Name |
        ForEach-Object { Get-Content $_.FullName -Raw | ConvertFrom-Json })
    if (-not $runs.Count) { return $null }

    return [ordered]@{
        runs = $runs.Count
        timer_median_ms = Median @($runs | ForEach-Object { $_.timer.median_ms })
        timer_p95_ms = Median @($runs | ForEach-Object { $_.timer.p95_ms })
        max_stall_ms = Median @($runs | ForEach-Object { $_.timer.max_stall_ms })
    }
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
        repeat = $Repeat
        branch = (& git -C $repoRoot branch --show-current).Trim()
        commit = (& git -C $repoRoot rev-parse HEAD).Trim()
    }
    $manifest | ConvertTo-Json | Set-Content (Result "manifest") -Encoding utf8

    $summary = [ordered]@{
        vanilla = BenchSummary "vanilla"
        qpsycopg = BenchSummary "qpsycopg"
        vanilla_qt = QtSummary "vanilla"
        qpsycopg_qt = QtSummary "qpsycopg"
    }
    $summary | ConvertTo-Json -Depth 4 | Set-Content (Result "summary") -Encoding utf8

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
        "all" { Smoke; Bench; QtBench; GuiBlock }
    }
    Save-Results
} finally {
    if ($null -eq $oldImpl) { Remove-Item Env:PSYCOPG_IMPL -ErrorAction Ignore }
    else { $env:PSYCOPG_IMPL = $oldImpl }

    if ($null -eq $oldWorker) { Remove-Item Env:QPSYCOPG_WORKER_IMPL -ErrorAction Ignore }
    else { $env:QPSYCOPG_WORKER_IMPL = $oldWorker }
}
