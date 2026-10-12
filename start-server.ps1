# Start llama-server for ghosttab.
#  -np 2        two slots: autocomplete keeps slot 0 and its cached prompt, while
#               dictation and the assistant use slot 1 (each gets half of -c)
#  mmproj       the multimodal projector is loaded so dictation can send audio;
#               pass -NoAudio to skip it when dictation is not used
#  -Background  start hidden, log to %LOCALAPPDATA%\ghosttab and return
# Uses a build unpacked under vendor\llama-b*\ if present, otherwise
# llama-server from PATH. Gemma 4 needs a recent build.
param(
    [string]$Model = "unsloth/gemma-4-E2B-it-GGUF:Q4_K_M",
    [int]$Port = 8089,
    [int]$Ctx = 16384,
    [switch]$NoAudio,
    [switch]$Background
)
$vendored = Get-ChildItem -Path "$PSScriptRoot\vendor\llama-b*\llama-server.exe" -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1
$server = if ($vendored) { $vendored.FullName } else { (Get-Command llama-server -ErrorAction Stop).Source }
$arguments = @("-hf", $Model, "--port", $Port, "-c", $Ctx, "-np", 2, "-ngl", 999, "--no-webui")
if ($NoAudio) { $arguments += "--no-mmproj" }

if (-not $Background) {
    & $server @arguments
    return
}
$logs = Join-Path $env:LOCALAPPDATA "ghosttab"
New-Item -ItemType Directory -Force $logs | Out-Null
foreach ($name in "llama-server.log", "llama-server.err.log") {
    $path = Join-Path $logs $name
    if (Test-Path $path) { Move-Item $path "$path.1" -Force }
}
Start-Process -FilePath $server -ArgumentList $arguments -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logs "llama-server.log") `
    -RedirectStandardError (Join-Path $logs "llama-server.err.log")
