<#
.SYNOPSIS
  Publish an existing completed DB dump to Drive and verify remote bytes by read-back.
.DESCRIPTION
  Does not create a new pg_dump, stop workers, restore a DB or authorize a writer handoff.
  Requires a configured rclone remote; a G: mount write is not upload acknowledgement.
#>
param(
    [Parameter(Mandatory=$true)][string]$Dump,
    [string]$Remote = 'gdrive:AEC-INTELLIGENCE/90_ARCHIVE/db-checkpoints',
    [string]$Staging = 'D:\AECData\checkpoint-staging',
    [string]$Checkpoint = '',
    [string]$RestoreReport = '',
    [string]$SourceCommit = '',
    [string]$Receipt = ''
)
. (Join-Path $PSScriptRoot '_common.ps1')
$python = Get-AecPython
$env:PYTHONPATH = (Join-Path $script:RepoRoot 'src') + ';' + $env:PYTHONPATH
$arguments = @('-m', 'aec_intelligence.operational.drive_checkpoint', 'publish',
               '--dump', $Dump, '--remote', $Remote, '--staging', $Staging)
if ($Checkpoint) { $arguments += @('--checkpoint', $Checkpoint) }
if ($RestoreReport) { $arguments += @('--restore-report', $RestoreReport) }
if ($SourceCommit) { $arguments += @('--source-commit', $SourceCommit) }
$ErrorActionPreference = 'Continue'
$result = & $python @arguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($Receipt) {
    $parent = Split-Path -Parent ([IO.Path]::GetFullPath($Receipt))
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Set-Content -LiteralPath $Receipt -Value ($result -join "`n") -Encoding UTF8
}
$result
