<#
.SYNOPSIS
  pg_dump (custom format) of the aec database into a backups folder, keeping the newest N dumps.
  Only files named aec-db-YYYYMMDDTHHMMSSZ.dump in that folder are ever rotated/deleted.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\ops\backup.ps1
  powershell -ExecutionPolicy Bypass -File scripts\ops\backup.ps1 -Target 'D:\AECData\backups' -Keep 30
#>
param(
    [string]$Target = '',
    [int]$Keep = 14,
    [string]$Container = 'aec-db',
    [switch]$UseLocalPgDump
)
. (Join-Path $PSScriptRoot '_common.ps1')

if (-not $Target) {
    $Target = if ($env:AEC_BACKUP_DIR) { $env:AEC_BACKUP_DIR } else { 'G:\내 드라이브\AEC-INTELLIGENCE\backups' }
}
$logDir = Join-Path $script:RepoRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ('backup-' + (Get-Date -Format 'yyyyMMdd') + '.log')

$cliArgs = @('backup', '--target', $Target, '--keep', "$Keep")
if (-not $UseLocalPgDump) { $cliArgs += @('--docker-container', $Container) }
try {
    Invoke-AecCli -Arguments $cliArgs 2>&1 | Tee-Object -FilePath $log -Append
    Write-Host "Backup OK -> $Target"
} catch {
    "[$(Get-Date -Format s)] BACKUP FAILED: $_" | Tee-Object -FilePath $log -Append
    exit 1
}
