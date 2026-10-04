<#
.SYNOPSIS
  Register (or remove) the per-user scheduled tasks that keep the bulk ingest running.
  No admin rights are needed. The tasks run only while the user is logged on.
    \AEC\AEC-Bulk-Workers  at logon; restarts every 5 min after a failure; runs bulk-run.ps1 -Role workers
    \AEC\AEC-Bulk-Census   at logon (resumes an interrupted census; finished sources are skipped)
    \AEC\AEC-Bulk-Census-Refresh  daily at 03:00 with -Refresh to pick up new or changed drawings
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\ops\register-bulk-tasks.ps1 -Start
  powershell -ExecutionPolicy Bypass -File scripts\ops\register-bulk-tasks.ps1 -Unregister
#>
param(
    [string]$Config = 'D:\AECData\bulk\sources.json',
    [int]$Workers = 2,
    [switch]$Start,
    [switch]$Unregister
)
$ErrorActionPreference = 'Stop'
$names = @('AEC-Bulk-Workers', 'AEC-Bulk-Census', 'AEC-Bulk-Census-Refresh')
if ($Unregister) {
    foreach ($n in $names) {
        Stop-ScheduledTask -TaskPath '\AEC\' -TaskName $n -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskPath '\AEC\' -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue
    }
    Write-Host 'Removed. Running python workers finish their current job; stop them with Stop-Process if needed.'
    return
}
$ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$script = Join-Path $PSScriptRoot 'bulk-run.ps1'
$user = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 5)
$common = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" -Config `"$Config`""

$workers = New-ScheduledTaskAction -Execute $ps -Argument "$common -Role workers -Workers $Workers"
Register-ScheduledTask -TaskPath '\AEC\' -TaskName 'AEC-Bulk-Workers' -Action $workers -Principal $principal `
    -Settings $settings -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $user) -Force | Out-Null

$census = @(
    (New-ScheduledTaskAction -Execute $ps -Argument "$common -Role census")
)
$censusDaily = New-ScheduledTaskAction -Execute $ps -Argument "$common -Role census -Refresh"
Register-ScheduledTask -TaskPath '\AEC\' -TaskName 'AEC-Bulk-Census' -Action $census -Principal $principal `
    -Settings $settings -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $user) -Force | Out-Null
Register-ScheduledTask -TaskPath '\AEC\' -TaskName 'AEC-Bulk-Census-Refresh' -Action $censusDaily -Principal $principal `
    -Settings $settings -Trigger (New-ScheduledTaskTrigger -Daily -At 3am) -Force | Out-Null

if ($Start) {
    Start-ScheduledTask -TaskPath '\AEC\' -TaskName 'AEC-Bulk-Census'
    Start-ScheduledTask -TaskPath '\AEC\' -TaskName 'AEC-Bulk-Workers'
}
Get-ScheduledTask -TaskPath '\AEC\' | Select-Object TaskName, State | Format-Table -AutoSize
