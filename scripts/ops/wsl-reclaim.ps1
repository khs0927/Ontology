<#
.SYNOPSIS
  Drop the Docker WSL VM's clean page cache, but ONLY when Windows is short of RAM
  (scheduled task \AEC\AEC-WSL-Reclaim, every 30 min; see register-host-tasks.ps1).
.DESCRIPTION
  %USERPROFILE%\.wslconfig caps the VM (memory=6GB, autoMemoryReclaim=dropCache). Postgres lives on
  the USB data disk, so an unconditional drop evicts the hot table/HNSW pages and turns every vector
  insert into random USB reads (ingest jobs went from ~10 s to minutes). Rules:
    * Windows "Available MBytes" (free + standby) below -MinAvailableMB, else skip;
    * at most one drop per -MinIntervalMin (the 20:22/20:38/21:08 drops each re-coldened the DB);
    * no `sync`: `echo 1 > drop_caches` only drops CLEAN page cache. A global sync flushed every dirty
      page of the VM onto the USB disk in the middle of a 6-minute Postgres checkpoint (22:01 incident,
      docs/OPERATIONS.ko.md "aec-db 크래시");
    * skip while the VM has more than -MaxDirtyMB of dirty/writeback pages (a checkpoint or a bulk
      write is in flight - dropping now only hurts and frees little).
  Postgres' own shared_buffers is shared memory, which drop_caches never evicts.
#>
param([int]$MinAvailableMB = 750, [int]$MinIntervalMin = 120, [int]$MaxDirtyMB = 64,
      [string]$Log = 'D:\AECData\bulk\logs\wsl-reclaim.log')
function Write-ReclaimLog([string]$Message) {
    "$(Get-Date -Format s) $Message" | Out-File $Log -Append -Encoding utf8
}
$availMB = [int](Get-Counter '\Memory\Available MBytes').CounterSamples[0].CookedValue
if ($availMB -ge $MinAvailableMB) { Write-ReclaimLog "avail=${availMB}MB -> skip"; return }

$last = $null
if (Test-Path -LiteralPath $Log) {
    $hit = Get-Content -LiteralPath $Log -Tail 200 | Where-Object { $_ -match '-> drop_caches' } | Select-Object -Last 1
    if ($hit) { try { $last = [datetime]::Parse(($hit -split ' ')[0]) } catch { $last = $null } }
}
if ($last -and ((Get-Date) - $last).TotalMinutes -lt $MinIntervalMin) {
    Write-ReclaimLog "avail=${availMB}MB -> skip (last drop $($last.ToString('HH:mm')), min interval ${MinIntervalMin} min)"
    return
}
# One container: measure Dirty+Writeback (kB) and drop only when it is small. No double quotes inside
# (Windows PowerShell 5.1 mangles them when passing arguments to native programs).
$maxKb = $MaxDirtyMB * 1024
$sh = 'd=$(awk ''/^(Dirty|Writeback):/{s+=$2} END{print s+0}'' /proc/meminfo); ' +
      "if [ `$d -gt $maxKb ]; then echo skip-dirty `$d; else echo 1 > /proc/sys/vm/drop_caches; echo dropped `$d; fi"
$out = (docker run --rm --privileged alpine sh -c $sh 2>&1 | Out-String).Trim()
if ($out -match '^dropped (\d+)') {
    Write-ReclaimLog "avail=${availMB}MB dirty=$([int]([int]$matches[1] / 1024))MB -> drop_caches"
} elseif ($out -match '^skip-dirty (\d+)') {
    Write-ReclaimLog "avail=${availMB}MB -> skip (VM dirty $([int]([int]$matches[1] / 1024)) MB > ${MaxDirtyMB} MB)"
} else {
    Write-ReclaimLog "avail=${availMB}MB -> error: $out"
}
