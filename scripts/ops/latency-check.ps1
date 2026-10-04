<#
.SYNOPSIS
  Latency check of the live stack: Graph RAG eval without the LLM (quality + retrieval p50/p95) and
  HTTP timings of POST /v1/search, /v1/ask (generate=false) and a few /v1/ask with the local LLM.
.DESCRIPTION
  Run it as a scheduled task or detached (it can take 10+ minutes while ingest/re-embed load the DB).
  The eval set is private (questions about real drawings) and stays outside git, e.g.
  D:\AECData\eval\graphrag-ko-50.jsonl (JSON lines with q, type, route). The token is read from .env
  and never printed. Output: <OutRoot>\<yyyyMMdd-HHmm>\run.log (+ graphrag.txt).
  Reference (2026-10-04, 512 docs, idle): search p50 ~2 s; graphrag no-LLM p50 <0.1 s, p95 <2 s;
  /v1/ask with qwen3:8b ~5-15 s.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\ops\latency-check.ps1 -EvalSet D:\AECData\eval\graphrag-ko-50.jsonl
#>
param(
    [Parameter(Mandatory = $true)][string]$EvalSet,
    [string]$OutRoot = 'D:\AECData\eval\latency',
    [string]$Api = 'http://127.0.0.1:58000',
    [int]$LlmQuestions = 6,
    [switch]$SkipEval
)
. (Join-Path $PSScriptRoot '_common.ps1')
$ErrorActionPreference = 'Continue'
$out = Join-Path $OutRoot (Get-Date -Format 'yyyyMMdd-HHmm'); New-Item -ItemType Directory -Force -Path $out | Out-Null
$log = Join-Path $out 'run.log'
function L($m) { $line = "$(Get-Date -Format s) $m"; $line | Out-File $log -Append -Encoding utf8; Write-Host $line }
Import-AecDotEnv -Path (Join-Path $script:RepoRoot '.env')
$env:PYTHONPATH = (Join-Path $script:RepoRoot 'src') + ';' + $env:PYTHONPATH
$env:PYTHONIOENCODING = 'utf-8'
$env:AEC_EMBEDDING_URL = if ($env:AEC_HOST_EMBEDDING_URL) { $env:AEC_HOST_EMBEDDING_URL } else { 'http://127.0.0.1:11434' }
$env:AEC_LLM_URL = $env:AEC_EMBEDDING_URL
if (-not $SkipEval) {
    Push-Location $out
    & (Get-AecPython) -m aec_intelligence.operational.cli graphrag-eval $EvalSet --no-llm *> (Join-Path $out 'graphrag.txt')
    Pop-Location
    try {
        $j = Get-Content (Join-Path $out 'graphrag.txt') -Raw -Encoding utf8 | ConvertFrom-Json
        L "graphrag-eval recall@10=$($j.recall_at_10) route=$($j.route_accuracy) citation_validity=$($j.citation_validity) refusal=$($j.refusal_accuracy) p50=$($j.retrieval_ms_p50) p95=$($j.retrieval_ms_p95) ms"
    } catch { L "graphrag-eval: no JSON summary (see graphrag.txt)" }
}
$h = @{ Authorization = "Bearer $env:AEC_API_TOKEN" }
$qs = @(Get-Content $EvalSet -Encoding utf8 | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json })
function Post($path, $obj, $timeout) {
    $b = [Text.Encoding]::UTF8.GetBytes(($obj | ConvertTo-Json -Compress))
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try { $r = Invoke-RestMethod "$Api$path" -Method Post -Headers $h -Body $b -ContentType 'application/json; charset=utf-8' -TimeoutSec $timeout } catch { $r = $null }
    [pscustomobject]@{ ms = [int]$sw.Elapsed.TotalMilliseconds; ok = [bool]$r; r = $r }
}
function Stat($name, $rows) {
    $ms = @($rows | ForEach-Object ms | Sort-Object)
    if (-not $ms.Count) { L "$name n=0"; return }
    $p = { param($q) $ms[[Math]::Min($ms.Count - 1, [int][Math]::Ceiling($q * $ms.Count) - 1)] }
    L "$name n=$($ms.Count) ok=$(@($rows | Where-Object ok).Count) p50=$(& $p 0.5) p95=$(& $p 0.95) max=$($ms[-1]) ms"
}
Stat 'POST /v1/search' @(foreach ($q in $qs) { Post '/v1/search' @{ query = $q.q; top_k = 10 } 120 })
Stat 'POST /v1/ask generate=false' @(foreach ($q in $qs) { Post '/v1/ask' @{ question = $q.q; top_k = 8; generate = $false } 120 })
$llm = @(foreach ($q in ($qs | Where-Object { $_.type -ne 'unanswerable' } | Select-Object -First $LlmQuestions)) { Post '/v1/ask' @{ question = $q.q; top_k = 8 } 300 })
Stat 'POST /v1/ask (local LLM)' $llm
L "done -> $out"
