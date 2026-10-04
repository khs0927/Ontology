<#
.SYNOPSIS
  Run `ollama serve` for the AEC stack (scheduled task \AEC\AEC-Ollama, see register-host-tasks.ps1).
.DESCRIPTION
  * Runtime and models on the NVMe disk (-OllamaHome, default C:\AECLocal\Ollama): on Windows+CUDA the
    model file is read in full on every load, and from the USB data disk (shared with Docker/Postgres)
    loads timed out ("GPU discovery watchdog timed out" -> Vulkan fallback or failed loads).
  * Two resident models (bge-m3 for ingest + qwen3:8b for answers), flash attention, q8_0 KV cache,
    unload after 10 min idle so the 8B model's ~10 GB commit is returned to Windows.
  * llama-server logs every slot at verbosity 4 (~100 MB/day): the log is rotated at start when it
    is larger than -RotateMB (one .1 generation kept).
  * Exits at once when a server already answers on 127.0.0.1:11434, so the 5-minute repeating
    trigger acts as a watchdog without starting a second server.
#>
param(
    [string]$OllamaHome = 'C:\AECLocal\Ollama',
    [string]$Log = 'D:\AECData\ollama-logs\serve.log',
    [int]$RotateMB = 200,
    [string]$KeepAlive = '10m'
)
$ErrorActionPreference = 'Continue'
try {
    Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:11434/api/version' -TimeoutSec 5 | Out-Null
    exit 0  # already serving
} catch { }
New-Item -ItemType Directory -Force -Path (Split-Path $Log) | Out-Null
if ((Test-Path -LiteralPath $Log) -and (Get-Item -LiteralPath $Log).Length -gt $RotateMB * 1MB) {
    Move-Item -LiteralPath $Log -Destination "$Log.1" -Force
}
$env:OLLAMA_MODELS = Join-Path $OllamaHome 'models'
$env:OLLAMA_HOST = '127.0.0.1:11434'
$env:OLLAMA_MAX_LOADED_MODELS = '2'
$env:OLLAMA_FLASH_ATTENTION = '1'
$env:OLLAMA_KV_CACHE_TYPE = 'q8_0'
$env:OLLAMA_KEEP_ALIVE = $KeepAlive
$exe = Join-Path $OllamaHome 'ollama.exe'
Add-Content -LiteralPath $Log -Value "$(Get-Date -Format s) [ollama-serve.ps1] start $exe models=$($env:OLLAMA_MODELS)" -Encoding UTF8
# cmd keeps the redirection simple and appends stdout+stderr of the long-running server.
& cmd.exe /c "`"$exe`" serve >> `"$Log`" 2>&1"
exit $LASTEXITCODE
