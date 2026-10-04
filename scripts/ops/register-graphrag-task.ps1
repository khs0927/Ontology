<#
.SYNOPSIS
  Registers \AEC\AEC-GraphRAG-Refresh: runs 'graphrag.ps1 refresh' every -EveryHours hours so the
  knowledge graph and community summaries follow the bulk ingest. Non-admin (current user, Limited).
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\ops\register-graphrag-task.ps1 -EveryHours 2
  Start-ScheduledTask -TaskPath '\AEC\' -TaskName 'AEC-GraphRAG-Refresh'
  Unregister-ScheduledTask -TaskPath '\AEC\' -TaskName 'AEC-GraphRAG-Refresh' -Confirm:$false
#>
param([int]$EveryHours = 2, [string]$TaskName = 'AEC-GraphRAG-Refresh')
$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'graphrag.ps1'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $repoRoot `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" refresh"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(10) -RepetitionInterval (New-TimeSpan -Hours $EveryHours)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) -Priority 7
Register-ScheduledTask -TaskPath '\AEC\' -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Force | Out-Null
Write-Host "Registered \AEC\$TaskName every $EveryHours h -> $script refresh"
