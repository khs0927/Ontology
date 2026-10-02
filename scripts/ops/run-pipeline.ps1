<#
.SYNOPSIS
  preflight -> (optional) embeddings -> census -> enqueue-census -> run-workers -> report,
  for one pilot folder or the whole drive. Safe to re-run: the census resumes, jobs are
  deduplicated by sha256.
.PARAMETER Workers
  Worker processes. 0 (default) = auto: CPU cores - 1, capped at (physical RAM / 2 GB), min 1.
.PARAMETER Embeddings
  Start the docker compose 'embeddings' profile (BAAI/bge-m3 via TEI on localhost:58080), wait
  until /health answers, and point AEC_EMBEDDING_URL at it so stored vectors are semantic.
.PARAMETER PreflightOnly
  Run the checks and exit.
.PARAMETER SkipPreflight
  Skip the checks (not recommended).
.PARAMETER MinFreeGB
  Minimum free space required on the AEC_DATA_ROOT drive (default 20).
.EXAMPLE
  # Pilot: one top-level project folder, at most 50 unique drawings
  powershell -ExecutionPolicy Bypass -File scripts\ops\run-pipeline.ps1 -Root 'G:\내 드라이브\도면' -OnlyFolder '현장A' -Limit 50
  # Everything, with real bge-m3 embeddings
  powershell -ExecutionPolicy Bypass -File scripts\ops\run-pipeline.ps1 -Root 'G:\내 드라이브\도면' -Embeddings
#>
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [string]$OnlyFolder = '',
    [int]$Limit = 0,
    [int]$Workers = 0,
    [string]$Out = '',
    [switch]$SkipCensus,
    [switch]$Embeddings,
    [int]$EmbeddingsTimeoutSec = 900,
    [switch]$SkipPreflight,
    [switch]$PreflightOnly,
    [int]$MinFreeGB = 20
)
. (Join-Path $PSScriptRoot '_common.ps1')

if (-not $Out) {
    $Out = if ($env:AEC_DATA_ROOT) { Join-Path $env:AEC_DATA_ROOT 'census' } else { 'D:\AECData\census' }
}

function Get-AecDefaultWorkers {
    $cores = [Environment]::ProcessorCount
    $byCpu = [Math]::Max(1, $cores - 1)
    $byRam = $byCpu
    try {
        $ramBytes = (Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop).TotalPhysicalMemory
        $byRam = [Math]::Max(1, [int][Math]::Floor($ramBytes / 2GB))
    } catch {
        Write-Warning "RAM size unavailable ($($_.Exception.Message)); using CPU count only."
    }
    $n = [Math]::Min($byCpu, $byRam)
    Write-Host "[workers] auto: cores=$cores -> $byCpu, RAM/2GB -> $byRam => $n"
    return $n
}

function Invoke-Docker {
    param([string[]]$Arguments)
    # docker writes progress on stderr; do not let 'Stop' turn that into an exception.
    $ErrorActionPreference = 'Continue'
    $output = & docker @Arguments 2>&1
    return [pscustomobject]@{ Code = $LASTEXITCODE; Output = ($output | Out-String).Trim() }
}

function Test-PathUnder {
    param([string]$Child, [string]$Parent)
    $c = [System.IO.Path]::GetFullPath($Child).TrimEnd('\', '/') + '\'
    $p = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\', '/') + '\'
    return $c.StartsWith($p, [System.StringComparison]::OrdinalIgnoreCase)
}

function Invoke-AecPreflight {
    $problems = New-Object System.Collections.Generic.List[string]
    function Ok([string]$m) { Write-Host "  [OK]   $m" }
    function Warn([string]$m) { Write-Host "  [WARN] $m" -ForegroundColor Yellow }
    function Fail([string]$m) { Write-Host "  [FAIL] $m" -ForegroundColor Red; $problems.Add($m) }
    Write-Host '[preflight]'

    # 1. Docker engine, 2. db container health
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Fail 'docker not found on PATH (install/start Docker Desktop)'
    } else {
        $info = Invoke-Docker @('info', '--format', '{{.ServerVersion}}')
        if ($info.Code -ne 0) { Fail "Docker engine not running: $($info.Output)" }
        else {
            Ok "Docker engine $($info.Output)"
            $health = Invoke-Docker @('inspect', '--format', '{{.State.Health.Status}}', 'aec-db')
            if ($health.Code -ne 0) { Fail 'container aec-db not found -> docker compose up -d db' }
            elseif ($health.Output -ne 'healthy') { Fail "aec-db health=$($health.Output) (expected healthy)" }
            else { Ok 'aec-db healthy' }
        }
    }

    # 3. DWG converter (mirrors aec_intelligence.dwg.select_dwg_converter)
    $mode = if ($env:AEC_DWG_CONVERTER) { $env:AEC_DWG_CONVERTER.Trim().ToLower() } else { 'auto' }
    $oda = $null
    if ($env:AEC_ODA_EXECUTABLE) {
        if (Test-Path -LiteralPath $env:AEC_ODA_EXECUTABLE -PathType Leaf) { $oda = $env:AEC_ODA_EXECUTABLE }
    } elseif (Get-Command ODAFileConverter -ErrorAction SilentlyContinue) { $oda = 'ODAFileConverter (PATH)' }
    $libre = $null
    if ($env:AEC_LIBREDWG_EXECUTABLE) {
        if (Test-Path -LiteralPath $env:AEC_LIBREDWG_EXECUTABLE -PathType Leaf) { $libre = $env:AEC_LIBREDWG_EXECUTABLE }
    } elseif (Get-Command dwg2dxf -ErrorAction SilentlyContinue) { $libre = (Get-Command dwg2dxf).Source }
    switch ($mode) {
        'oda' {
            if ($oda) { Ok "DWG converter: ODA ($oda)" }
            else { Fail "AEC_DWG_CONVERTER=oda but ODA not found (AEC_ODA_EXECUTABLE='$($env:AEC_ODA_EXECUTABLE)')" }
        }
        'libredwg' {
            if ($libre) { Ok "DWG converter: LibreDWG ($libre)" }
            else { Fail "AEC_DWG_CONVERTER=libredwg but dwg2dxf not found (AEC_LIBREDWG_EXECUTABLE='$($env:AEC_LIBREDWG_EXECUTABLE)')" }
        }
        'auto' {
            if ($env:AEC_ODA_EXECUTABLE -and -not $oda) { Warn "AEC_ODA_EXECUTABLE set but missing: $($env:AEC_ODA_EXECUTABLE)" }
            if ($oda) { Ok "DWG converter: ODA ($oda)" }
            elseif ($libre) { Ok "DWG converter: LibreDWG fallback ($libre)" }
            else { Fail 'no DWG converter: set AEC_ODA_EXECUTABLE or install LibreDWG dwg2dxf (AEC_LIBREDWG_EXECUTABLE)' }
        }
        default { Fail "AEC_DWG_CONVERTER='$mode' (expected auto, oda or libredwg)" }
    }

    # 4. AEC_IMPORT_ROOTS must contain the census root
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) { Fail "Root not found: $Root" }
    $roots = @()
    if ($env:AEC_IMPORT_ROOTS) { $roots = @($env:AEC_IMPORT_ROOTS.Split(';') | Where-Object { $_.Trim() }) }
    $covered = $false
    foreach ($r in $roots) { if (Test-PathUnder -Child $Root -Parent $r.Trim()) { $covered = $true; break } }
    if ($covered) { Ok "AEC_IMPORT_ROOTS covers $Root" }
    else { Fail "AEC_IMPORT_ROOTS does not contain $Root (enqueue-census would skip every file). Current: '$($env:AEC_IMPORT_ROOTS)'" }

    # 5. Free disk on AEC_DATA_ROOT
    $dataRoot = if ($env:AEC_DATA_ROOT) { $env:AEC_DATA_ROOT } else { 'D:\AECData' }
    try {
        $qualifier = Split-Path -Qualifier ([System.IO.Path]::GetFullPath($dataRoot))
        $drive = New-Object System.IO.DriveInfo($qualifier)
        $freeGB = [Math]::Round($drive.AvailableFreeSpace / 1GB, 1)
        if ($freeGB -lt $MinFreeGB) { Fail "only $freeGB GB free on $qualifier (AEC_DATA_ROOT=$dataRoot, need $MinFreeGB GB)" }
        else { Ok "$freeGB GB free on $qualifier (AEC_DATA_ROOT=$dataRoot)" }
    } catch { Warn "free space check failed for $dataRoot : $($_.Exception.Message)" }

    if ($problems.Count -gt 0) {
        throw ("preflight failed ($($problems.Count)):`n - " + ($problems -join "`n - "))
    }
}

function Start-AecEmbeddings {
    Write-Host '[embeddings] docker compose --profile embeddings up -d embeddings'
    $compose = Join-Path $script:RepoRoot 'docker-compose.yml'
    $r = Invoke-Docker @('compose', '-f', $compose, '--profile', 'embeddings', 'up', '-d', 'embeddings')
    if ($r.Code -ne 0) { throw "failed to start embeddings: $($r.Output)" }
    # Host-side URL (the compose service publishes 58080 -> 80); containers use http://embeddings:80.
    $base = if ($env:AEC_EMBEDDINGS_HOST_URL) { $env:AEC_EMBEDDINGS_HOST_URL.TrimEnd('/') } else { 'http://localhost:58080' }
    $deadline = (Get-Date).AddSeconds($EmbeddingsTimeoutSec)
    Write-Host "[embeddings] waiting for $base/health (first start downloads bge-m3, ~2.3 GB)"
    while ($true) {
        try {
            $resp = Invoke-WebRequest -Uri "$base/health" -UseBasicParsing -TimeoutSec 5
            if ($resp.StatusCode -eq 200) { break }
        } catch { }
        if ((Get-Date) -gt $deadline) { throw "embeddings not healthy after $EmbeddingsTimeoutSec s (see: docker logs aec-embeddings)" }
        Start-Sleep -Seconds 5
    }
    $env:AEC_EMBEDDING_URL = $base
    Write-Host "[embeddings] ready; AEC_EMBEDDING_URL=$base"
}

if (-not $SkipPreflight) { Invoke-AecPreflight }
if ($PreflightOnly) { Write-Host 'Preflight passed.'; return }
if ($Embeddings) { Start-AecEmbeddings }
elseif (-not $env:AEC_EMBEDDING_URL) {
    Write-Warning 'AEC_EMBEDDING_URL is empty: vectors use the offline hash model (not semantic). Add -Embeddings for bge-m3.'
}
if ($Workers -le 0) { $Workers = Get-AecDefaultWorkers }

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
