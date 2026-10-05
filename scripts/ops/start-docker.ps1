<#
.SYNOPSIS
  Start Docker Desktop once at user logon, without restarting an existing instance.
.DESCRIPTION
  Register as a per-user logon task, not a recurring watchdog. An existing Desktop or backend
  process counts as a start in progress and is left alone. This helper does not wait for the
  engine, touch containers/settings, or require the data drive to be mounted.
  Logs live on the user's local profile so they are available before D: or Google Drive.
#>
param(
    [string]$DockerPath = (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'),
    [string]$Log = (Join-Path $env:LOCALAPPDATA 'AEC\logs\docker-logon.log')
)
$ErrorActionPreference = 'Stop'

function Write-StartupLog([string]$Message) {
    $line = "$(Get-Date -Format s) $Message"
    try {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Log) | Out-Null
        Add-Content -LiteralPath $Log -Value $line -Encoding UTF8
    } catch {
        Write-Warning "could not write Docker logon log: $_"
    }
    Write-Host $line
}

# A backend can still be booting while the Desktop UI has not appeared. Never relaunch either.
$existing = @(Get-Process -Name 'Docker Desktop', 'com.docker.backend' -ErrorAction SilentlyContinue)
if ($existing.Count) {
    Write-StartupLog "skip: Docker Desktop/backend already exists ($($existing.Count) process(es)); no restart"
    exit 0
}
if (-not (Test-Path -LiteralPath $DockerPath -PathType Leaf)) {
    Write-StartupLog "failed: Docker Desktop executable not found at $DockerPath"
    exit 1
}
try {
    Start-Process -FilePath $DockerPath -WindowStyle Hidden -ErrorAction Stop
    Write-StartupLog 'Docker Desktop start requested; engine and data-drive readiness must be checked by the caller'
    exit 0
} catch {
    Write-StartupLog "failed: Docker Desktop start request: $_"
    exit 1
}
