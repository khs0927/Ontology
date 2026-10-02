# Shared helpers for the AEC batch scripts (dot-source this file).
# Loads <repo>\.env (KEY=VALUE lines) into the process environment and forces UTF-8 so Korean
# paths and output survive Windows PowerShell 5.1 and the legacy console code page.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$script:RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

function Import-AecDotEnv {
    param([string]$Path = (Join-Path $script:RepoRoot '.env'))
    if (-not (Test-Path -LiteralPath $Path)) { return }
    foreach ($line in [System.IO.File]::ReadAllLines($Path, [System.Text.Encoding]::UTF8)) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#') -or -not $trimmed.Contains('=')) { continue }
        $key, $value = $trimmed.Split('=', 2)
        $value = $value.Trim().Trim('"').Trim("'")
        [Environment]::SetEnvironmentVariable($key.Trim(), $value, 'Process')
    }
}

function Get-AecPython {
    if ($env:AEC_PYTHON) { return $env:AEC_PYTHON }
    $venv = Join-Path $script:RepoRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) { return $venv }
    return 'python'
}

# Usage: Invoke-AecCli -Arguments @('census', $root, '--out', $out)
# (pass one array: '-n'/'--out' tokens would otherwise be taken as PowerShell parameters)
function Invoke-AecCli {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $python = Get-AecPython
    $env:PYTHONPATH = (Join-Path $script:RepoRoot 'src') + ';' + $env:PYTHONPATH
    # Python logs progress on stderr; with 'Stop', Windows PowerShell 5.1 would treat that as an error.
    $ErrorActionPreference = 'Continue'
    & $python -m aec_intelligence.operational.cli @Arguments
    if ($LASTEXITCODE -ne 0) { throw "aec cli failed ($LASTEXITCODE): $($Arguments -join ' ')" }
}

Import-AecDotEnv
