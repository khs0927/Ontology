<#
.SYNOPSIS
  Registers a daily Windows Scheduled Task that runs backup.ps1. Nothing is registered unless you
  run this script yourself (once). The task runs as the current user because Google Drive for
  Desktop's G: drive only exists inside the user's logon session.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\ops\register-backup-task.ps1 -At '03:00'
  Start-ScheduledTask -TaskName 'AEC-DB-Backup'                          # run once now to test
  Unregister-ScheduledTask -TaskName 'AEC-DB-Backup' -Confirm:$false     # remove again
#>
param(
    [string]$TaskName = 'AEC-DB-Backup',
    [string]$At = '03:00',
    [string]$Target = '',
    [int]$Keep = 14
)
$ErrorActionPreference = 'Stop'
$backupScript = Join-Path $PSScriptRoot 'backup.ps1'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$backupScript`" -Keep $Keep"
if ($Target) { $arguments += " -Target `"$Target`"" }

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force |
    Out-Null
Write-Host "Registered scheduled task '$TaskName' daily at $At -> $backupScript"
Write-Host "Test it now with: Start-ScheduledTask -TaskName '$TaskName'"
