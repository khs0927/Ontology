<#
.SYNOPSIS
  Resume the whole AEC pipeline unattended after a reboot or sleep (Docker -> DB -> Ollama -> RAM gate
  -> workers -> backfill-storey [-> kg-build] -> job summary). Every step is idempotent; safe to re-run.
.DESCRIPTION
  1. Docker Desktop is started only when the engine does not answer; then `docker compose up -d db`
     (repo compose file) and wait until aec-db is healthy. No container/volume is ever removed.
  2. Ollama (C:\AECLocal) is started via ollama-serve.ps1 (hidden) when /api/version does not answer.
  3. Free-RAM gate: waits until at least -GbPerWorker GB are free, then
     workers = min(configured, floor(freeGB / GbPerWorker)).
  4. -Resume renames the STOP-WORKERS gate to STOP-WORKERS.resumed-<timestamp> (never deleted).
     Without -Resume an existing gate makes the script stop after the summary.
  5. One drain round of `run-workers` with the same host environment as bulk-run.ps1 (skipped when the
     AEC-bulk-workers mutex is held, i.e. the scheduled worker loop is already running), then
     `backfill-storey --batch-size 5000`; with -AfterDrain also `kg-build --force`.
  6. `ingest-stats` summary (job counts by state).
  Never prints the DB password (the .env is loaded into the process only). G:\ is only read.
  Logs: <LogDir>\resume-all-yyyyMMdd.log
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\ops\resume-all.ps1 -Resume
  powershell -ExecutionPolicy Bypass -File scripts\ops\resume-all.ps1 -Resume -AfterDrain
#>
param(
    [switch]$Resume,
    [switch]$AfterDrain,
    [string]$Config = 'D:\AECData\bulk\sources.json',
    [int]$Workers = 2,
    [double]$GbPerWorker = 2,
    [int]$MemoryWaitMin = 60,
    [int]$DockerWaitMin = 10,
    [int]$OllamaWaitMin = 5,
    [string]$LogDir = 'D:\AECData\logs',
    [string]$DockerPath = (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'),
    [string]$OllamaHome = 'C:\AECLocal\Ollama'
)
. (Join-Path $PSScriptRoot '_common.ps1')
$ErrorActionPreference = 'Continue'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$logFile = Join-Path $LogDir "resume-all-$(Get-Date -Format yyyyMMdd).log"

function Write-ResumeLog([string]$Message) {
    $line = "$(Get-Date -Format s) [resume-all] $Message"
    Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
    Write-Host $line
}

function Invoke-Cli([string[]]$CliArgs) {
    Write-ResumeLog "cli: $($CliArgs -join ' ')"
    & $python -m aec_intelligence.operational.cli @CliArgs 2>&1 | ForEach-Object {
        Add-Content -LiteralPath $logFile -Value "$_" -Encoding UTF8
        Write-Host "$_"
    }
    Write-ResumeLog "exit code $LASTEXITCODE"
    return $LASTEXITCODE
}

function Test-DockerEngine {
    & docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Test-Ollama {
    try { Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:11434/api/version' -TimeoutSec 5 | Out-Null; return $true }
    catch { return $false }
}

function Get-FreeGb {
    $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
    return [double]$os.FreePhysicalMemory / 1MB  # KB -> GB
}

$mutex = New-Object System.Threading.Mutex($false, 'AEC-resume-all')
if (-not $mutex.WaitOne(0)) { Write-Host 'resume-all already running'; exit 0 }
Write-ResumeLog "start pid=$PID Resume=$Resume AfterDrain=$AfterDrain"

# --- 1. Docker + aec-db ----------------------------------------------------------------------
if (-not (Test-DockerEngine)) {
    $existing = @(Get-Process -Name 'Docker Desktop', 'com.docker.backend' -ErrorAction SilentlyContinue)
    if ($existing.Count) { Write-ResumeLog 'docker engine down but Desktop is starting; waiting' }
    elseif (Test-Path -LiteralPath $DockerPath) { Start-Process -FilePath $DockerPath -WindowStyle Hidden; Write-ResumeLog 'Docker Desktop start requested' }
    else { Write-ResumeLog "failed: Docker Desktop not found at $DockerPath"; exit 1 }
    $deadline = (Get-Date).AddMinutes($DockerWaitMin)
    while (-not (Test-DockerEngine)) {
        if ((Get-Date) -gt $deadline) { Write-ResumeLog "failed: docker engine not ready in $DockerWaitMin min"; exit 1 }
        Start-Sleep -Seconds 10
    }
}
Write-ResumeLog 'docker engine up'
Push-Location $script:RepoRoot
try { & docker compose up -d db 2>&1 | ForEach-Object { Add-Content -LiteralPath $logFile -Value "$_" -Encoding UTF8 } }
finally { Pop-Location }
$deadline = (Get-Date).AddMinutes($DockerWaitMin)
while ($true) {
    $health = (& docker inspect --format '{{.State.Health.Status}}' aec-db 2>$null) -join ''
    if ($health -eq 'healthy') { break }
    if ((Get-Date) -gt $deadline) { Write-ResumeLog "failed: aec-db not healthy (state '$health')"; exit 1 }
    Start-Sleep -Seconds 5
}
Write-ResumeLog 'aec-db healthy'

# --- 2. Ollama -------------------------------------------------------------------------------
if (-not (Test-Ollama)) {
    $serve = Join-Path $PSScriptRoot 'ollama-serve.ps1'
    Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$serve`"", '-OllamaHome', "`"$OllamaHome`"")
    Write-ResumeLog 'ollama-serve.ps1 started'
    $deadline = (Get-Date).AddMinutes($OllamaWaitMin)
    while (-not (Test-Ollama)) {
        if ((Get-Date) -gt $deadline) { Write-ResumeLog "failed: Ollama API not ready in $OllamaWaitMin min"; exit 1 }
        Start-Sleep -Seconds 5
    }
}
Write-ResumeLog 'ollama API up'

# --- 3. Free-RAM gate ------------------------------------------------------------------------
$configured = $Workers
try {
    $cfg = [System.IO.File]::ReadAllText($Config, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
    if ($cfg.workers -and [int]$cfg.workers -ge 1) { $configured = [int]$cfg.workers }
} catch { Write-ResumeLog "could not read $Config ($_); stopping"; exit 1 }
$deadline = (Get-Date).AddMinutes($MemoryWaitMin)
while ($true) {
    $freeGb = 0
    try { $freeGb = Get-FreeGb } catch { Write-ResumeLog "memory query failed: $_" }
    $n = [math]::Min($configured, [int][math]::Floor($freeGb / $GbPerWorker))
    if ($n -ge 1) { break }
    if ((Get-Date) -gt $deadline) { Write-ResumeLog "failed: free RAM $([math]::Round($freeGb,1)) GB < $GbPerWorker GB for $MemoryWaitMin min"; exit 2 }
    Write-ResumeLog "waiting: free RAM $([math]::Round($freeGb,1)) GB < $GbPerWorker GB"
    Start-Sleep -Seconds 60
}
Write-ResumeLog "free RAM $([math]::Round($freeGb,1)) GB -> $n worker(s) (configured $configured)"

# --- 4. Stop gate ----------------------------------------------------------------------------
$stopFile = if ($env:AEC_WORKER_STOP_FILE) { $env:AEC_WORKER_STOP_FILE } else {
    Join-Path $(if ($env:AEC_DATA_ROOT) { $env:AEC_DATA_ROOT } else { 'D:\AECData' }) 'bulk\STOP-WORKERS' }
$gated = Test-Path -LiteralPath $stopFile
if ($gated -and $Resume) {
    $renamed = "$stopFile.resumed-$(Get-Date -Format yyyyMMdd-HHmmss)"
    Move-Item -LiteralPath $stopFile -Destination $renamed
    Write-ResumeLog "stop gate renamed to $renamed"
    $gated = $false
} elseif ($gated) { Write-ResumeLog "stop gate $stopFile present (use -Resume to lift it); skipping workers" }

# --- 5. Workers, backfill, kg-build ----------------------------------------------------------
$roots = @($cfg.sources | ForEach-Object { $_.roots } | Sort-Object -Unique)
$env:AEC_IMPORT_ROOTS = ($roots -join ';')
$env:AEC_EMBEDDING_URL = if ($env:AEC_HOST_EMBEDDING_URL) { $env:AEC_HOST_EMBEDDING_URL } else { 'http://127.0.0.1:11434' }
if (-not $env:AEC_OCR_MODEL_DIR) { $env:AEC_OCR_MODEL_DIR = 'D:\AECData\ocr-models' }
if (-not $env:AEC_OCR_THREADS) { $env:AEC_OCR_THREADS = '2' }
if (-not $env:AEC_MIN_FREE_GB -and $cfg.min_free_gb) {
    $env:AEC_MIN_FREE_GB = (($cfg.min_free_gb.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ';')
}
$env:PYTHONPATH = (Join-Path $script:RepoRoot 'src') + ';' + $env:PYTHONPATH
$python = Get-AecPython
try { (Get-Process -Id $PID).PriorityClass = 'BelowNormal' } catch { }

if (-not $gated) {
    $workerMutex = New-Object System.Threading.Mutex($false, 'AEC-bulk-workers')
    if ($workerMutex.WaitOne(0)) {
        try { [void](Invoke-Cli @('run-workers', '--processes', "$n", '--queue', $(if ($cfg.queue) { $cfg.queue } else { 'cad' }))) }
        finally { $workerMutex.ReleaseMutex() }
    } else { Write-ResumeLog 'bulk worker loop already running (AEC-Bulk-Workers); not starting a second one' }
    [void](Invoke-Cli @('backfill-storey', '--batch-size', '5000'))
    if ($AfterDrain) { [void](Invoke-Cli @('kg-build', '--force')) }
}

# --- 6. Summary ------------------------------------------------------------------------------
[void](Invoke-Cli @('ingest-stats'))
Write-ResumeLog 'done'
exit 0
