<#
.SYNOPSIS
  Backup/restore drill: restore a pg_dump (custom format) into a SCRATCH database inside the aec-db
  container and compare exact row counts of every table (aec.*, kg_*, the AGE graph schemas, ag_catalog)
  with the live database. The live database is only read. The scratch database is dropped afterwards
  unless -KeepScratch.
.DESCRIPTION
  Counts only match exactly when nothing writes to the live DB between the dump and the comparison:
  drain the workers first (stop-workers.ps1 -Drain) or accept that jobs/objects/embeddings drift.
  -Backup takes a fresh dump with backup.ps1 into -Target first.
  Needs free space inside the Docker VM roughly equal to the live DB size (SELECT pg_database_size('aec')).
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\ops\restore-drill.ps1 -Backup -Target D:\AECData\backups
  powershell -ExecutionPolicy Bypass -File scripts\ops\restore-drill.ps1 -Dump D:\AECData\backups\aec-db-20261004T051140Z.dump
#>
param(
    [string]$Dump = '',
    [switch]$Backup,
    [string]$Target = 'D:\AECData\backups',
    [string]$Container = 'aec-db',
    [string]$Scratch = 'aec_restore_drill',
    [string]$Report = '',
    [switch]$KeepScratch
)
. (Join-Path $PSScriptRoot '_common.ps1')
$ErrorActionPreference = 'Stop'
if ($Scratch -notmatch '^aec_[a-z0-9_]+$' -or $Scratch -eq 'aec') { throw "scratch db name must look like aec_<name> (got '$Scratch')" }

if ($Backup) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'backup.ps1') -Target $Target -Container $Container
    if ($LASTEXITCODE -ne 0) { throw 'backup failed' }
    $Dump = (Get-ChildItem -LiteralPath $Target -Filter 'aec-db-*.dump' | Sort-Object Name | Select-Object -Last 1).FullName
}
if (-not $Dump -or -not (Test-Path -LiteralPath $Dump)) { throw "dump not found: '$Dump' (pass -Dump or -Backup)" }

# Exact count of every base table in the aec schema, the AGE graph schemas and ag_catalog.
$countSql = @"
SELECT t.table_schema || '.' || t.table_name || '=' ||
       (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I', t.table_schema, t.table_name), false, true, '')))[1]::text
FROM information_schema.tables t
WHERE t.table_type = 'BASE TABLE'
  AND (t.table_schema IN ('aec', 'ag_catalog') OR t.table_schema LIKE 'aec\_%')
ORDER BY 1
"@
function Get-Counts([string]$db) {
    $rows = & docker exec $Container psql -U aec -d $db -At -v ON_ERROR_STOP=1 -c $countSql
    if ($LASTEXITCODE -ne 0) { throw "count query failed on $db" }
    $map = [ordered]@{}
    foreach ($r in $rows) { $k, $v = $r.Split('=', 2); $map[$k] = [long]$v }
    $map
}

$ErrorActionPreference = 'Continue'
$start = Get-Date
Write-Host "dump: $Dump ($([math]::Round((Get-Item -LiteralPath $Dump).Length/1MB,1)) MB)"
& docker exec $Container dropdb -U aec --if-exists $Scratch 2>&1 | Out-Null
& docker exec $Container createdb -U aec $Scratch
if ($LASTEXITCODE -ne 0) { throw "createdb $Scratch failed" }
$restoreErr = Join-Path $env:TEMP "restore-drill-$PID.err"
& docker cp $Dump "${Container}:/tmp/restore-drill.dump" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'docker cp of the dump failed' }
$t = Measure-Command {
    & docker exec $Container pg_restore -U aec -d $Scratch --no-owner /tmp/restore-drill.dump 2> $restoreErr
}
$restoreExit = $LASTEXITCODE
$errs = @(Get-Content -LiteralPath $restoreErr -ErrorAction SilentlyContinue | Where-Object { $_ -match 'error' })
Write-Host "pg_restore exit=$restoreExit in $([int]$t.TotalSeconds)s; error lines: $($errs.Count)"
$errs | Select-Object -First 10 | ForEach-Object { Write-Host "  $_" }

$live = Get-Counts 'aec'
$rest = Get-Counts $Scratch
$diff = @()
foreach ($k in (@($live.Keys) + @($rest.Keys) | Sort-Object -Unique)) {
    $a = $live[$k]; $b = $rest[$k]
    if ($a -ne $b) { $diff += [pscustomobject]@{ table = $k; live = $a; restored = $b } }
}
$key = 'aec.documents', 'aec.objects', 'aec.relations', 'aec.embeddings', 'aec.jobs', 'aec.kg_nodes', 'aec.kg_edges', 'aec.kg_aliases', 'ag_catalog.ag_graph', 'ag_catalog.ag_label'
$summary = [ordered]@{
    dump = $Dump; scratch = $Scratch; restore_seconds = [int]$t.TotalSeconds; restore_exit = $restoreExit
    restore_error_lines = $errs.Count; tables_live = $live.Count; tables_restored = $rest.Count
    rows_live = ($live.Values | Measure-Object -Sum).Sum; rows_restored = ($rest.Values | Measure-Object -Sum).Sum
    key_tables = [ordered]@{}; differing_tables = $diff; result = $(if ($diff.Count -eq 0 -and $errs.Count -eq 0) { 'MATCH' } else { 'DIFF' })
    finished = (Get-Date -Format s)
}
foreach ($k in $key) { $summary.key_tables[$k] = "$($live[$k]) / $($rest[$k])" }
$json = $summary | ConvertTo-Json -Depth 5
if ($Report) { Set-Content -LiteralPath $Report -Value $json -Encoding UTF8 }
$json
if (-not $KeepScratch) { & docker exec $Container dropdb -U aec $Scratch 2>&1 | Out-Null }
& docker exec $Container rm -f /tmp/restore-drill.dump 2>&1 | Out-Null
Remove-Item -LiteralPath $restoreErr -ErrorAction SilentlyContinue
if ($summary.result -ne 'MATCH') { exit 2 }
