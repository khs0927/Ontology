<#
.SYNOPSIS
  Register the per-user host tasks around the bulk ingest (no admin rights; run while logged on):
    \AEC\AEC-Ollama       ollama serve from -OllamaHome (NVMe), models in <OllamaHome>\models, log rotation;
                          logon trigger + every 5 min (IgnoreNew = watchdog), restart 999x / 1 min
    \AEC\AEC-Reembed      reembed.ps1 at logon + every 30 min (IgnoreNew; exits at once when nothing is
                          pending, when the stop file exists, or when another re-embed holds the mutex)
    \AEC\AEC-WSL-Reclaim  wsl-reclaim.ps1 every 30 min (drops the VM page cache only when Windows is short of RAM)
.PARAMETER Only
  Register just these tasks (Ollama, Reembed, WslReclaim). Default: all three.
  Re-registering AEC-Ollama restarts nothing by itself, but the next trigger starts the new action
  only after the running server exits; restart it with Stop-/Start-ScheduledTask when convenient.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\ops\register-host-tasks.ps1 -Only Reembed -Start
  powershell -ExecutionPolicy Bypass -File scripts\ops\register-host-tasks.ps1 -OllamaHome C:\AECLocal\Ollama
#>
param(
    [ValidateSet('Ollama', 'Reembed', 'WslReclaim')][string[]]$Only = @('Ollama', 'Reembed', 'WslReclaim'),
    [string]$OllamaHome = 'C:\AECLocal\Ollama',
    [string]$OllamaLog = 'D:\AECData\ollama-logs\serve.log',
    [int]$ReembedEveryMin = 30,
    [switch]$Start
)
$ErrorActionPreference = 'Stop'
$ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$user = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$registered = @()

function Register-AecTask($Name, $Script, $ScriptArgs, $Triggers, $Settings) {
    $action = New-ScheduledTaskAction -Execute $ps -WorkingDirectory $repoRoot `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $PSScriptRoot $Script)`" $ScriptArgs"
    Register-ScheduledTask -TaskPath '\AEC\' -TaskName $Name -Action $action -Principal $principal `
        -Settings $Settings -Trigger $Triggers -Force | Out-Null
    $script:registered += $Name
}

function Repeating([int]$Minutes) {
    # A daily trigger repeating every N minutes for 1 day = "every N minutes, forever".
    $t = New-ScheduledTaskTrigger -Daily -At '00:00'
    $t.Repetition = (New-ScheduledTaskTrigger -Once -At '00:00' -RepetitionInterval (New-TimeSpan -Minutes $Minutes) `
        -RepetitionDuration (New-TimeSpan -Days 1)).Repetition
    $t
}

if ($Only -contains 'Ollama') {
    if (-not (Test-Path -LiteralPath (Join-Path $OllamaHome 'ollama.exe'))) { throw "ollama.exe not found in $OllamaHome" }
    $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1)
    Register-AecTask 'AEC-Ollama' 'ollama-serve.ps1' "-OllamaHome `"$OllamaHome`" -Log `"$OllamaLog`"" `
        @((New-ScheduledTaskTrigger -AtLogOn -User $user), (Repeating 5)) $s
}
if ($Only -contains 'Reembed') {
    $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 5)
    Register-AecTask 'AEC-Reembed' 'reembed.ps1' '' @((New-ScheduledTaskTrigger -AtLogOn -User $user), (Repeating $ReembedEveryMin)) $s
}
if ($Only -contains 'WslReclaim') {
    $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew
    Register-AecTask 'AEC-WSL-Reclaim' 'wsl-reclaim.ps1' '' @(Repeating 30) $s
}
if ($Start) { foreach ($n in $registered) { Start-ScheduledTask -TaskPath '\AEC\' -TaskName $n } }
Get-ScheduledTask -TaskPath '\AEC\' | Select-Object TaskName, State | Format-Table -AutoSize
