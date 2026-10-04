<#
.SYNOPSIS
  Gracefully drain the host ingest workers (for deploys), then optionally resume them.
.DESCRIPTION
  -Drain  : create the stop file. Each worker process finishes the job it is running, then exits;
            bulk-run.ps1 -Role workers exits instead of starting a new round. Waits until no
            'run-workers' / worker child process is left (or -TimeoutMin), then reports RUNNING jobs.
            Stopping the scheduled task directly kills only the parent: spawn children kept claiming jobs
            with the old code. (Children now also exit when their parent is gone.)
  -Resume : remove the stop file and start the \AEC\AEC-Bulk-Workers task.
  Long jobs (large DWG/PDF) can take 10+ minutes; a job killed mid-run is retried after its lease expires.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\ops\stop-workers.ps1 -Drain -TimeoutMin 30
  git pull; docker compose build api; docker compose run --rm migrate; docker compose up -d api
  powershell -ExecutionPolicy Bypass -File scripts\ops\stop-workers.ps1 -Resume
#>
param(
    [switch]$Drain,
    [switch]$Resume,
    [int]$TimeoutMin = 30,
    [string]$TaskPath = '\AEC\',
    [string]$TaskName = 'AEC-Bulk-Workers'
)
. (Join-Path $PSScriptRoot '_common.ps1')
$ErrorActionPreference = 'Stop'
$root = if ($env:AEC_DATA_ROOT) { $env:AEC_DATA_ROOT } else { 'D:\AECData' }
$stopFile = if ($env:AEC_WORKER_STOP_FILE) { $env:AEC_WORKER_STOP_FILE } else { Join-Path $root 'bulk\STOP-WORKERS' }

function Get-LeasePids {
    # Worker ids are census-<pid>-<n>: the pids holding RUNNING jobs (also finds orphaned children).
    $owners = & docker exec aec-db psql -U aec -d aec -At -c "SELECT lease_owner FROM aec.jobs WHERE state='RUNNING'" 2>$null
    @($owners | ForEach-Object { if ($_ -match '^census-(\d+)-\d+$') { [int]$Matches[1] } })
}

function Get-WorkerProcesses {
    $py = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'")
    $parents = @($py | Where-Object { $_.CommandLine -match 'operational\.cli run-workers' })
    $parentIds = @($parents | ForEach-Object { [int]$_.ProcessId })
    $leasePids = Get-LeasePids
    $children = @($py | Where-Object {
        ($_.CommandLine -match 'spawn_main\(parent_pid=(\d+)' -and $parentIds -contains [int]$Matches[1]) -or
        ($leasePids -contains [int]$_.ProcessId)
    })
    @($parents + $children | Sort-Object ProcessId -Unique)
}

if ($Drain) {
    New-Item -ItemType Directory -Force -Path (Split-Path $stopFile) | Out-Null
    Set-Content -LiteralPath $stopFile -Value "drain requested $(Get-Date -Format s) by $env:USERNAME" -Encoding UTF8
    Write-Host "stop file: $stopFile"
    $deadline = (Get-Date).AddMinutes($TimeoutMin)
    while ((Get-Date) -lt $deadline) {
        $procs = Get-WorkerProcesses
        if ($procs.Count -eq 0) { break }
        Write-Host "$(Get-Date -Format T) waiting for $($procs.Count) worker process(es) to finish their job"
        Start-Sleep -Seconds 15
    }
    $left = Get-WorkerProcesses
    if ($left.Count) {
        Write-Warning "$($left.Count) worker process(es) still running after $TimeoutMin min (long job). Re-run -Drain to keep waiting."
        exit 1
    }
    Write-Host 'all workers drained'
    exit 0
}
if ($Resume) {
    if (Test-Path -LiteralPath $stopFile) { Remove-Item -LiteralPath $stopFile -Force }
    Start-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName
    Write-Host "resumed $TaskPath$TaskName"
    exit 0
}
Write-Host 'usage: stop-workers.ps1 -Drain [-TimeoutMin 30] | -Resume'
exit 2
