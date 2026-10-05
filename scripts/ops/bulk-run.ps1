<#
.SYNOPSIS
  Durable, throttled bulk ingest on the Windows host. Run by the scheduled tasks that
  register-bulk-tasks.ps1 creates, or by hand. Safe to start twice: a second copy of the same
  role exits at once.
.PARAMETER Role
  census  - 'bulk-census' over every source in sources.json (resumable; enqueues as it goes).
            With -Refresh, finished sources are walked again so new or changed drawings are picked up.
  workers - 'run-workers' in a loop: drain the queue, sleep -IdleSleepSec, repeat. Exits only when the
            stop file exists (scripts/ops/stop-workers.ps1: graceful drain for deploys).
.PARAMETER Config
  sources.json (private: it names the drawing folders). See load_bulk_config in census.py.
.NOTES
  Host overrides: AEC_EMBEDDING_URL=http://127.0.0.1:11434 (the .env value is for containers),
  AEC_IMPORT_ROOTS = every source root, OCR models in D:\AECData\ocr-models, 2 OCR threads,
  AEC_MIN_FREE_GB from sources.json "min_free_gb" (census and workers pause while a drive is low).
  Workers wait before starting each round for 2048 MB available host RAM; AEC_MIN_AVAILABLE_MB
  overrides this threshold. A failed memory query also pauses the launch until the next retry.
  The process runs at BelowNormal priority; ODA/OCR child processes inherit it.
  Worker count: sources.json "workers" (re-read before every round) overrides -Workers, so it can be
  lowered while a re-embed or another heavy job runs. A round (run-workers) lasts until the queue is
  empty, so to apply a change now: stop-workers.ps1 -Drain, then -Resume.
  Logs: <LogDir>\<role>-yyyyMMdd.log
#>
param(
    [Parameter(Mandatory = $true)][ValidateSet('census', 'workers')][string]$Role,
    [string]$Config = 'D:\AECData\bulk\sources.json',
    [int]$Workers = 2,
    [int]$IdleSleepSec = 300,
    [string]$LogDir = 'D:\AECData\bulk\logs',
    [ValidateSet('Idle', 'BelowNormal', 'Normal')][string]$Priority = 'BelowNormal',
    [switch]$Refresh
)
. (Join-Path $PSScriptRoot '_common.ps1')
$ErrorActionPreference = 'Continue'

$mutex = New-Object System.Threading.Mutex($false, "AEC-bulk-$Role")
if (-not $mutex.WaitOne(0)) { Write-Host "AEC bulk $Role already running"; exit 0 }

try { (Get-Process -Id $PID).PriorityClass = $Priority } catch { }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-BulkLog([string]$Message) {
    $line = "$(Get-Date -Format s) [$Role] $Message"
    Add-Content -LiteralPath (Join-Path $LogDir "$Role-$(Get-Date -Format yyyyMMdd).log") -Value $line -Encoding UTF8
}

$cfg = [System.IO.File]::ReadAllText($Config, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
$roots = @($cfg.sources | ForEach-Object { $_.roots } | Sort-Object -Unique)
$env:AEC_IMPORT_ROOTS = ($roots -join ';')
$env:AEC_EMBEDDING_URL = if ($env:AEC_HOST_EMBEDDING_URL) { $env:AEC_HOST_EMBEDDING_URL } else { 'http://127.0.0.1:11434' }
if (-not $env:AEC_OCR_MODEL_DIR) { $env:AEC_OCR_MODEL_DIR = 'D:\AECData\ocr-models' }
if (-not $env:AEC_OCR_THREADS) { $env:AEC_OCR_THREADS = '2' }
# Workers pause while a drive is below its floor (Google Drive streams files through a cache on C:).
if (-not $env:AEC_MIN_FREE_GB -and $cfg.min_free_gb) {
    $env:AEC_MIN_FREE_GB = (($cfg.min_free_gb.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ';')
}
$env:PYTHONPATH = (Join-Path $script:RepoRoot 'src') + ';' + $env:PYTHONPATH
$python = Get-AecPython

function Invoke-Logged([string[]]$CliArgs) {
    $log = Join-Path $LogDir "$Role-$(Get-Date -Format yyyyMMdd).log"
    Write-BulkLog "start: $($CliArgs -join ' ')"
    & $python -m aec_intelligence.operational.cli @CliArgs 2>&1 | ForEach-Object {
        Add-Content -LiteralPath $log -Value "$_" -Encoding UTF8
    }
    Write-BulkLog "exit code $LASTEXITCODE"
    return $LASTEXITCODE
}

Write-BulkLog "pid $PID priority $Priority roots=$($roots.Count) embedding=$($env:AEC_EMBEDDING_URL)"
# Drain also blocks census starts, so scheduled refreshes cannot enqueue during maintenance.
$stopFile = if ($env:AEC_WORKER_STOP_FILE) { $env:AEC_WORKER_STOP_FILE } else {
    Join-Path $(if ($env:AEC_DATA_ROOT) { $env:AEC_DATA_ROOT } else { 'D:\AECData' }) 'bulk\STOP-WORKERS' }
if (Test-Path -LiteralPath $stopFile) { Write-BulkLog "stop file $stopFile present: exiting"; exit 0 }
if ($Role -eq 'census') {
    $cliArgs = @('bulk-census', '--config', $Config)
    if ($Refresh) { $cliArgs += '--refresh' }
    $code = Invoke-Logged $cliArgs
    exit $code
}
# Drain switch shared with the Python workers (census.worker_stop_file): scripts/ops/stop-workers.ps1.
$minAvailableMb = 2048
if ($env:AEC_MIN_AVAILABLE_MB) {
    $configuredMin = 0
    if ([int]::TryParse($env:AEC_MIN_AVAILABLE_MB, [ref]$configuredMin) -and $configuredMin -gt 0) {
        $minAvailableMb = $configuredMin
    } else {
        Write-BulkLog "invalid AEC_MIN_AVAILABLE_MB; using $minAvailableMb MB"
    }
}
while ($true) {
    if (Test-Path -LiteralPath $stopFile) { Write-BulkLog "stop file $stopFile present: exiting"; exit 0 }
    try {
        $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
        if ($null -eq $os.FreePhysicalMemory) { throw 'available memory reading missing' }
        $availableMb = [double]$os.FreePhysicalMemory / 1024
        if ($availableMb -lt $minAvailableMb) {
            Write-BulkLog "paused: available memory $([math]::Round($availableMb)) MB below $minAvailableMb MB"
            Start-Sleep -Seconds $IdleSleepSec
            continue
        }
    } catch {
        Write-BulkLog "paused: could not read available memory ($_); retry in $IdleSleepSec seconds"
        Start-Sleep -Seconds $IdleSleepSec
        continue
    }
    $n = $Workers
    try {
        $live = [System.IO.File]::ReadAllText($Config, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
        if ($live.workers -and [int]$live.workers -ge 1) { $n = [int]$live.workers }
    } catch { Write-BulkLog "could not re-read $Config ($_); using $n worker(s)" }
    [void](Invoke-Logged @('run-workers', '--processes', "$n", '--queue', ($(if ($cfg.queue) { $cfg.queue } else { 'cad' }))))
    Start-Sleep -Seconds $IdleSleepSec
}
