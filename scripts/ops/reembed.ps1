<#
.SYNOPSIS
  Fill pending/missing vectors (cli reembed) on the host, gently: low priority, small batches,
  chunk retry with backoff, single instance, resumable (every chunk is committed; a re-run only
  embeds what is still missing).
.DESCRIPTION
  Ingest jobs store objects with embeddings pending when Ollama is busy or down; this catches up.
  Ollama serves one request at a time per model, so the re-embed and the bulk workers share it:
  lower the bulk worker count first (D:\AECData\bulk\sources.json "workers": 1) while this runs.
  Logs: <LogDir>\reembed-yyyyMMdd.log (progress lines "[reembed] written/pending").
  Durable: \AEC\AEC-Reembed (register-host-tasks.ps1) starts this at logon and every 30 min; a run
  exits at once while the worker stop file exists (deploys/migrations) or another re-embed runs.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\ops\reembed.ps1
  powershell -ExecutionPolicy Bypass -File scripts\ops\reembed.ps1 -BatchSize 8 -Pause 1 -DryRun
#>
param(
    [int]$BatchSize = 16,
    [double]$Pause = 0.5,
    [int]$ChunkRetries = 12,
    [double]$TimeoutSec = 180,
    [string]$Project = '',
    [string]$LogDir = 'D:\AECData\bulk\logs',
    [ValidateSet('Idle', 'BelowNormal', 'Normal')][string]$Priority = 'BelowNormal',
    [switch]$DryRun
)
. (Join-Path $PSScriptRoot '_common.ps1')
$ErrorActionPreference = 'Continue'
$mutex = New-Object System.Threading.Mutex($false, 'AEC-reembed')
if (-not $mutex.WaitOne(0)) { Write-Host 'AEC reembed already running'; exit 0 }
try { (Get-Process -Id $PID).PriorityClass = $Priority } catch { }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$log = Join-Path $LogDir "reembed-$(Get-Date -Format yyyyMMdd).log"
$stopFile = if ($env:AEC_WORKER_STOP_FILE) { $env:AEC_WORKER_STOP_FILE } else {
    Join-Path $(if ($env:AEC_DATA_ROOT -and (Test-Path -LiteralPath $env:AEC_DATA_ROOT)) { $env:AEC_DATA_ROOT } else { 'D:\AECData' }) 'bulk\STOP-WORKERS' }
if (Test-Path -LiteralPath $stopFile) {
    Add-Content -LiteralPath $log -Value "$(Get-Date -Format s) stop file $stopFile present: not starting" -Encoding UTF8
    exit 0
}

Import-AecDotEnv -Path (Join-Path $script:RepoRoot '.env')
# The .env URL is for containers (host.docker.internal); the host talks to Ollama on loopback.
$env:AEC_EMBEDDING_URL = if ($env:AEC_HOST_EMBEDDING_URL) { $env:AEC_HOST_EMBEDDING_URL } else { 'http://127.0.0.1:11434' }
$env:PYTHONPATH = (Join-Path $script:RepoRoot 'src') + ';' + $env:PYTHONPATH
$env:PYTHONIOENCODING = 'utf-8'
$python = Get-AecPython

$cliArgs = @('reembed', '--batch-size', "$BatchSize", '--pause', "$Pause", '--chunk-retries', "$ChunkRetries",
             '--timeout', "$TimeoutSec")
if ($Project) { $cliArgs += @('--project', $Project) }
if ($DryRun) { $cliArgs += '--dry-run' }
Add-Content -LiteralPath $log -Value "$(Get-Date -Format s) start pid $PID $($cliArgs -join ' ')" -Encoding UTF8
& $python -m aec_intelligence.operational.cli @cliArgs 2>&1 | ForEach-Object {
    Add-Content -LiteralPath $log -Value "$(Get-Date -Format s) $_" -Encoding UTF8
}
$code = $LASTEXITCODE
Add-Content -LiteralPath $log -Value "$(Get-Date -Format s) exit code $code" -Encoding UTF8
exit $code
