# Start llama-server for ghosttab.
#  -np 2   two slots: autocomplete keeps slot 0 and its cached prompt, while
#          dictation and the assistant use slot 1 (each gets half of -c)
#  mmproj  the multimodal projector is loaded so dictation can send audio;
#          pass -NoAudio to skip it when dictation is not used
# Uses a build unpacked under vendor\llama-b*\ if present, otherwise
# llama-server from PATH. Gemma 4 needs a recent build.
param(
    [string]$Model = "unsloth/gemma-4-E2B-it-GGUF:Q4_K_M",
    [int]$Port = 8089,
    [int]$Ctx = 16384,
    [switch]$NoAudio
)
$vendored = Get-ChildItem -Path "$PSScriptRoot\vendor\llama-b*\llama-server.exe" -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1
$server = if ($vendored) { $vendored.FullName } else { "llama-server" }
$extra = @()
if ($NoAudio) { $extra += "--no-mmproj" }
& $server -hf $Model --port $Port -c $Ctx -np 2 -ngl 999 --no-webui @extra
