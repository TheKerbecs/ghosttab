# Start llama-server for ghosttab.
#  -np 1        a single slot, so every request reuses the same KV cache
#  --no-mmproj  text only; skip the vision projector -hf would otherwise fetch
# Uses a build unpacked under vendor\llama-b*\ if present, otherwise
# llama-server from PATH. Gemma 4 needs a recent build.
param(
    [string]$Model = "unsloth/gemma-4-E2B-it-GGUF:Q4_K_M",
    [int]$Port = 8089,
    [int]$Ctx = 8192
)
$vendored = Get-ChildItem -Path "$PSScriptRoot\vendor\llama-b*\llama-server.exe" -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1
$server = if ($vendored) { $vendored.FullName } else { "llama-server" }
& $server -hf $Model --no-mmproj --port $Port -c $Ctx -np 1 -ngl 999 --no-webui
