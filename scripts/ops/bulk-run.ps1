<#
.SYNOPSIS
  Durable, throttled bulk ingest on the Windows host. Run by the scheduled tasks that
  register-bulk-tasks.ps1 creates, or by hand. Safe to start twice: a second copy of the same
  role exits at once.
.PARAMETER Role
  census  - 'bulk-census' over every source in sources.json (resumable; enqueues as it goes).
            With -Refresh, finished sources are walked again so new or changed drawings are picked up.
  workers - 'run-workers' in a loop: drain the queue, sleep -IdleSleepSec, repeat. Never exits.
.PARAMETER Config
  sources.json (private: it names the drawing folders). See load_bulk_config in census.py.
.NOTES
  Host overrides: AEC_EMBEDDING_URL=http://127.0.0.1:11434 (the .env value is for containers),
  AEC_IMPORT_ROOTS = every source root, OCR models in D:\AECData\ocr-models, 2 OCR threads,
  AEC_MIN_FREE_GB from sources.json "min_free_gb" (census and workers pause while a drive is low).
  The process runs at BelowNormal priority; ODA/OCR child processes inherit it.
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
if ($Role -eq 'census') {
    $cliArgs = @('bulk-census', '--config', $Config)
    if ($Refresh) { $cliArgs += '--refresh' }
    $code = Invoke-Logged $cliArgs
    exit $code
}
while ($true) {
    [void](Invoke-Logged @('run-workers', '--processes', "$Workers", '--queue', ($(if ($cfg.queue) { $cfg.queue } else { 'cad' }))))
    Start-Sleep -Seconds $IdleSleepSec
}
