<#
.SYNOPSIS
  Superseded by register-host-tasks.ps1 -Only GraphRag (logon + every N h, IgnoreNew, mutex, yields to
  interactive queries). Kept as a thin wrapper so existing runbooks keep working.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\ops\register-graphrag-task.ps1 -EveryHours 2
  Start-ScheduledTask -TaskPath '\AEC\' -TaskName 'AEC-GraphRAG-Refresh'
#>
param([int]$EveryHours = 2)
& (Join-Path $PSScriptRoot 'register-host-tasks.ps1') -Only GraphRag -GraphRagEveryHours $EveryHours
