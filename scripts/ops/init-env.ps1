<#
.SYNOPSIS
  Create or complete <repo>\.env with generated secrets (token policy).

.DESCRIPTION
  - .env missing  -> copied from .env.example; AEC_DB_PASSWORD (and the password inside
    AEC_DATABASE_URL) and AEC_API_TOKEN are generated.
  - .env present  -> only an empty/missing AEC_API_TOKEN is generated. Existing values are never
    changed (rotating AEC_DB_PASSWORD would lock you out of an initialised Postgres volume).
  - -PowerCadEnv <path> also writes POWERCAD_ONTOLOGY_TOKEN=<same token> into that file
    (created if missing, existing other lines kept, previous file backed up to *.bak-<time>).
  Secrets are never printed; .env is gitignored.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\ops\init-env.ps1
  powershell -ExecutionPolicy Bypass -File scripts\ops\init-env.ps1 -PowerCadEnv C:\CODE\power-cad-mcp\.env
#>
param(
    [string]$EnvPath,
    [string]$PowerCadEnv,
    [switch]$RotateToken
)
$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $EnvPath) { $EnvPath = Join-Path $RepoRoot '.env' }
$utf8 = New-Object System.Text.UTF8Encoding($false)

function New-Secret([int]$Bytes = 32) {
    $buf = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buf) } finally { $rng.Dispose() }
    # URL-safe base64 without padding: safe in .env, URLs and HTTP headers.
    return [Convert]::ToBase64String($buf).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Read-Lines([string]$Path) {
    if (Test-Path -LiteralPath $Path) { return [System.Collections.Generic.List[string]]([System.IO.File]::ReadAllLines($Path, $utf8)) }
    return New-Object System.Collections.Generic.List[string]
}

function Get-Value($Lines, [string]$Key) {
    foreach ($l in $Lines) { if ($l -match "^\s*$([regex]::Escape($Key))\s*=(.*)$") { return $Matches[1].Trim().Trim('"').Trim("'") } }
    return $null
}

function Set-Value($Lines, [string]$Key, [string]$Value) {
    for ($i = 0; $i -lt $Lines.Count; $i++) {
        if ($Lines[$i] -match "^\s*$([regex]::Escape($Key))\s*=") { $Lines[$i] = "$Key=$Value"; return }
    }
    $Lines.Add("$Key=$Value")
}

$created = $false
if (-not (Test-Path -LiteralPath $EnvPath)) {
    $example = Join-Path $RepoRoot '.env.example'
    if (-not (Test-Path -LiteralPath $example)) { throw ".env.example not found at $example" }
    Copy-Item -LiteralPath $example -Destination $EnvPath
    $created = $true
}
$lines = Read-Lines $EnvPath

if ($created) {
    $pw = New-Secret 24
    Set-Value $lines 'AEC_DB_PASSWORD' $pw
    $url = Get-Value $lines 'AEC_DATABASE_URL'
    if ($url -and $url -match '^(postgres(?:ql)?://[^:/@]+:)[^@]*(@.*)$') {
        Set-Value $lines 'AEC_DATABASE_URL' ($Matches[1] + $pw + $Matches[2])
    }
    Write-Host "Created $EnvPath (generated AEC_DB_PASSWORD; review AEC_HOST_DATA_ROOT / AEC_IMPORT_ROOTS)."
}

$token = Get-Value $lines 'AEC_API_TOKEN'
if (-not $token -or $RotateToken) {
    $token = New-Secret 32
    Set-Value $lines 'AEC_API_TOKEN' $token
    Write-Host "Generated AEC_API_TOKEN in $EnvPath (value not shown)."
} else {
    Write-Host "AEC_API_TOKEN already set in $EnvPath (unchanged; use -RotateToken to replace)."
}
[System.IO.File]::WriteAllLines($EnvPath, $lines, $utf8)

if ($PowerCadEnv) {
    $pcLines = Read-Lines $PowerCadEnv
    if (Test-Path -LiteralPath $PowerCadEnv) {
        Copy-Item -LiteralPath $PowerCadEnv -Destination ("$PowerCadEnv.bak-" + (Get-Date -Format 'yyyyMMddHHmmss'))
    }
    Set-Value $pcLines 'POWERCAD_ONTOLOGY_TOKEN' $token
    [System.IO.File]::WriteAllLines($PowerCadEnv, $pcLines, $utf8)
    Write-Host "Wrote POWERCAD_ONTOLOGY_TOKEN (= AEC_API_TOKEN) to $PowerCadEnv."
}
