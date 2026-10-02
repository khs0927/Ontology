<#
.SYNOPSIS
  census -> enqueue-census -> run-workers -> report, for one pilot folder or the whole drive.
  Safe to re-run: the census resumes, jobs are deduplicated by sha256.
.EXAMPLE
  # Pilot: one top-level project folder, at most 50 unique drawings
  powershell -ExecutionPolicy Bypass -File scripts\ops\run-pipeline.ps1 -Root 'G:\내 드라이브\도면' -OnlyFolder '현장A' -Limit 50
  # Everything
  powershell -ExecutionPolicy Bypass -File scripts\ops\run-pipeline.ps1 -Root 'G:\내 드라이브\도면' -Workers 4
#>
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [string]$OnlyFolder = '',
    [int]$Limit = 0,
    [int]$Workers = 3,
    [string]$Out = '',
    [switch]$SkipCensus
)
. (Join-Path $PSScriptRoot '_common.ps1')

if (-not $Out) {
    $Out = if ($env:AEC_DATA_ROOT) { Join-Path $env:AEC_DATA_ROOT 'census' } else { 'D:\AECData\census' }
}
if (-not $SkipCensus) {
    $census = @('census', $Root, '--out', $Out, '--resume')
    if ($OnlyFolder) { $census += @('--only-folder', $OnlyFolder) }
    Invoke-AecCli -Arguments $census
}
$enqueue = @('enqueue-census', $Out)
if ($OnlyFolder) { $enqueue += @('--only-folder', $OnlyFolder) }
if ($Limit -gt 0) { $enqueue += @('--limit', "$Limit") }
Invoke-AecCli -Arguments $enqueue
Invoke-AecCli -Arguments @('run-workers', '-n', "$Workers", '--out', (Join-Path $Out 'runs'))
Invoke-AecCli -Arguments @('report', '--out', (Join-Path $Out 'report'), '--census', $Out)
Write-Host "Report: $(Join-Path $Out 'report\report.md')"
