<#
.SYNOPSIS
  Drop the Docker WSL VM's clean page cache, but ONLY when Windows is short of RAM
  (scheduled task \AEC\AEC-WSL-Reclaim, every 30 min; see register-host-tasks.ps1).
.DESCRIPTION
  %USERPROFILE%\.wslconfig caps the VM (memory=6GB, autoMemoryReclaim=dropCache). Postgres lives on
  the USB data disk, so an unconditional drop evicts the hot table/HNSW pages and turns every vector
  insert into random USB reads (ingest jobs went from ~10 s to minutes). Threshold: Windows
  "Available MBytes" (free + standby) below -MinAvailableMB.
#>
param([int]$MinAvailableMB = 750, [string]$Log = 'D:\AECData\bulk\logs\wsl-reclaim.log')
$availMB = [int](Get-Counter '\Memory\Available MBytes').CounterSamples[0].CookedValue
if ($availMB -lt $MinAvailableMB) {
    docker run --rm --privileged alpine sh -c "sync; echo 1 > /proc/sys/vm/drop_caches" *> $null
    "$(Get-Date -Format s) avail=${availMB}MB -> drop_caches" | Out-File $Log -Append -Encoding utf8
} else {
    "$(Get-Date -Format s) avail=${availMB}MB -> skip" | Out-File $Log -Append -Encoding utf8
}
