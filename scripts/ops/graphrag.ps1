<#
.SYNOPSIS
  Knowledge graph + Graph RAG maintenance on the Windows host (Phase 4). Loads the repo .env, points
  the embedding/LLM clients at the host Ollama (127.0.0.1:11434) and runs one CLI step.
.PARAMETER Step
  refresh   - kg-build (only changed projects; fingerprint skip) then kg-summarize (cached, resumable;
              FAILED communities are retried) then kg-stats. Both steps yield to interactive API
              queries (AEC_INTERACTIVE_YIELD_SECONDS). Scheduled by register-host-tasks.ps1 -Only GraphRag.
  build     - kg-build only           summarize - kg-summarize only      stats - kg-stats
  ask       - one question (-Question, optional -Project, -NoLlm)
  eval      - graphrag-eval on -EvalFile (private golden set on the data disk)
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\ops\graphrag.ps1 refresh
  powershell -ExecutionPolicy Bypass -File scripts\ops\graphrag.ps1 ask -Question '2층 실 목록 알려줘'
.NOTES
  Every step is safe to interrupt and re-run: kg-build commits per project, kg-summarize per community.
  A named mutex keeps two refreshes from overlapping. Log: <LogDir>\graphrag-yyyyMMdd.log (files older
  than -KeepLogDays are deleted at the start of a refresh).
#>
param(
    [Parameter(Mandatory = $true)][ValidateSet('refresh', 'build', 'summarize', 'stats', 'ask', 'eval')][string]$Step,
    [string]$Question = '',
    [string]$Project = '',
    [switch]$NoLlm,
    [string]$EvalFile = 'D:\AECData\eval\graphrag-ko-50.jsonl',
    [string]$LogDir = 'D:\AECData\bulk\logs',
    [string]$SteelCatalog = 'C:\HS-STEEL\HSSTEEL\attributes',
    [int]$KeepLogDays = 14
)
. (Join-Path $PSScriptRoot '_common.ps1')
$ErrorActionPreference = 'Continue'
# The .env values are for containers (host.docker.internal); the host talks to Ollama on loopback.
$env:AEC_EMBEDDING_URL = 'http://127.0.0.1:11434'
$env:AEC_LLM_URL = 'http://127.0.0.1:11434'
if (-not $env:AEC_STEEL_CATALOG_DIR -and (Test-Path -LiteralPath $SteelCatalog)) { $env:AEC_STEEL_CATALOG_DIR = $SteelCatalog }

function Invoke-Step([string[]]$CliArgs) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $log = Join-Path $LogDir "graphrag-$(Get-Date -Format yyyyMMdd).log"
    Add-Content -LiteralPath $log -Value "$(Get-Date -Format s) $($CliArgs -join ' ')" -Encoding UTF8
    Invoke-AecCli -Arguments $CliArgs 2>&1 | Tee-Object -FilePath $log -Append
}

switch ($Step) {
    'refresh' {
        $mutex = New-Object System.Threading.Mutex($false, 'AEC-graphrag-refresh')
        if (-not $mutex.WaitOne(0)) { Write-Host 'graphrag refresh already running'; exit 0 }
        try {
            Get-ChildItem -LiteralPath $LogDir -Filter 'graphrag-*.log' -ErrorAction SilentlyContinue |
                Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepLogDays) } | Remove-Item -ErrorAction SilentlyContinue
            Invoke-Step @('kg-build'); Invoke-Step @('kg-summarize'); Invoke-Step @('kg-stats')
        } finally { $mutex.ReleaseMutex() }
    }
    'build' { Invoke-Step @('kg-build') }
    'summarize' { Invoke-Step @('kg-summarize') }
    'stats' { Invoke-AecCli -Arguments @('kg-stats') }
    'ask' {
        if (-not $Question) { throw '-Question is required' }
        $a = @('ask', $Question)
        if ($Project) { $a += @('--project', $Project) }
        if ($NoLlm) { $a += '--no-llm' }
        Invoke-AecCli -Arguments $a
    }
    'eval' {
        $a = @('graphrag-eval', $EvalFile)
        if ($NoLlm) { $a += '--no-llm' }
        Invoke-Step $a
    }
}
