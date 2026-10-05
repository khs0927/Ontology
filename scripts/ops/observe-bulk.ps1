<#
.SYNOPSIS
  Append one read-only host/DB observation. Scheduling is a separate operation.
.DESCRIPTION
  Does not start/stop containers, change tasks, or mutate the database. Each Docker CLI invocation
  has a timeout; only the CLI process created by this script is killed on timeout. Unavailable
  observations are null with an error, never reported as healthy or as a zero-length queue.
  Repeated samples support A/C/F and 24-hour stability review; samples alone do not prove continuity.
.EXAMPLE
  powershell -NoProfile -File scripts\ops\observe-bulk.ps1 -Once
#>
[CmdletBinding()]
param(
    [switch]$Once,
    [string]$LogPath = 'D:\AECData\bulk\logs\stability.jsonl',
    [ValidateRange(1, 60)][int]$DockerTimeoutSec = 20
)
$ErrorActionPreference = 'Stop'

function Invoke-BoundedDocker {
    param([string]$Arguments)
    $cliProcess = $null
    try {
        $dockerPath = (Get-Command docker -ErrorAction Stop).Source
        # Retain the process handle from creation. Start-Process -PassThru in Windows PowerShell
        # can expose a null ExitCode for a fast-exiting child whose handle was acquired too late.
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = $dockerPath
        $startInfo.Arguments = $Arguments
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $cliProcess = New-Object System.Diagnostics.Process
        $cliProcess.StartInfo = $startInfo
        if (-not $cliProcess.Start()) { throw 'Docker CLI could not start' }
        $null = $cliProcess.Handle
        # Drain both pipes concurrently so a full stderr/stdout buffer cannot block the child.
        $stdoutRead = $cliProcess.StandardOutput.ReadToEndAsync()
        $stderrRead = $cliProcess.StandardError.ReadToEndAsync()
        if (-not $cliProcess.WaitForExit($DockerTimeoutSec * 1000)) {
            # This handle belongs only to our Docker CLI, not to the engine/container or another CLI.
            try { $cliProcess.Kill() } catch { }
            return [pscustomobject]@{ ok = $false; output = $null; error = "Docker CLI timed out after $DockerTimeoutSec seconds"; exit_code = $null }
        }
        $cliProcess.Refresh()
        if (-not $stdoutRead.Wait(1000) -or -not $stderrRead.Wait(1000)) {
            throw 'Docker CLI output stream did not close after process exit'
        }
        $stdout = $stdoutRead.Result.Trim()
        $stderr = $stderrRead.Result.Trim()
        if ($cliProcess.ExitCode -ne 0) {
            if (-not $stderr) { $stderr = "Docker CLI exited with code $($cliProcess.ExitCode)" }
            if ($stderr.Length -gt 1200) { $stderr = $stderr.Substring(0, 1200) }
            return [pscustomobject]@{ ok = $false; output = $null; error = $stderr; exit_code = $cliProcess.ExitCode }
        }
        return [pscustomobject]@{ ok = $true; output = $stdout; error = $null; exit_code = 0 }
    } catch {
        return [pscustomobject]@{ ok = $false; output = $null; error = $_.Exception.Message; exit_code = $null }
    } finally {
        if ($cliProcess) { $cliProcess.Dispose() }
    }
}

$sample = [ordered]@{
    schema = 'aec-bulk-observation/1'
    timestamp = (Get-Date).ToString('o')
    host_boot = $null
    free_ram_mb = $null
    memory_error = $null
    db_status = $null
    db_health = $null
    db_started_at = $null
    docker_error = $null
    database_readable = $false
    database_error = $null
    jobs_by_state = $null
    ingestion_completed_last_hour = $null
    throughput_source = 'aec.metrics:ingestion_completed.created_at'
    observation_finished_at = $null
}
try {
    $os = Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 10 -ErrorAction Stop
    if ($null -eq $os.FreePhysicalMemory) { throw 'available RAM reading missing' }
    $sample.host_boot = $os.LastBootUpTime.ToString('o')
    $sample.free_ram_mb = [math]::Round([double]$os.FreePhysicalMemory / 1024, 1)
} catch { $sample.memory_error = $_.Exception.Message }

$state = Invoke-BoundedDocker 'inspect --format "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.State.StartedAt}}" aec-db'
if ($state.ok) {
    $parts = $state.output -split '\|', 3
    if ($parts.Count -eq 3) {
        $sample.db_status = $parts[0]
        $sample.db_health = $parts[1]
        $sample.db_started_at = $parts[2]
    } else { $sample.docker_error = 'Unexpected DB container state output' }
} else { $sample.docker_error = $state.error }

if ($sample.db_status -eq 'running') {
    # Local container socket authentication: no password, DSN, filenames, or document payloads logged.
    # Statement timeout bounds server-side work independently of the Docker CLI deadline.
    $query = @'
SET statement_timeout='5000ms'; SELECT json_build_object('observed_at', now(), 'jobs_by_state', (SELECT coalesce(json_object_agg(state,n),'{}'::json) FROM (SELECT state,count(*) AS n FROM aec.jobs GROUP BY state) j), 'ingestion_completed_last_hour', (SELECT count(*) FROM aec.metrics WHERE kind='ingestion_completed' AND created_at > now()-interval '1 hour'));
'@
    $database = Invoke-BoundedDocker ('exec aec-db psql -X -q -t -A -U aec -d aec -v ON_ERROR_STOP=1 -c "' + $query.Trim() + '"')
    if ($database.ok) {
        try {
            $counts = $database.output | ConvertFrom-Json -ErrorAction Stop
            if ($null -eq $counts.jobs_by_state -or $null -eq $counts.ingestion_completed_last_hour) {
                throw 'Database observation missing required counts'
            }
            $sample.database_readable = $true
            $sample.jobs_by_state = $counts.jobs_by_state
            $sample.ingestion_completed_last_hour = $counts.ingestion_completed_last_hour
            $sample.database_observed_at = $counts.observed_at
        } catch { $sample.database_error = $_.Exception.Message }
    } else { $sample.database_error = $database.error }
} else { $sample.database_error = 'DB query skipped: container was not observed running' }

$sample.observation_finished_at = (Get-Date).ToString('o')
$logDirectory = Split-Path -Parent ([IO.Path]::GetFullPath($LogPath))
[void][IO.Directory]::CreateDirectory($logDirectory)
$line = $sample | ConvertTo-Json -Depth 6 -Compress
[IO.File]::AppendAllText([IO.Path]::GetFullPath($LogPath), $line + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
Write-Output $line
# Default and -Once both observe exactly once. There is no implicit infinite polling loop.
