# Run ghosttab in the background: starts llama-server if it is not running,
# waits until it is ready, then starts ghosttab without a console window.
# Logs are written to %LOCALAPPDATA%\ghosttab.
#
#   .\ghosttab.ps1          start
#   .\ghosttab.ps1 -Stop    stop ghosttab and the llama-server it uses
param(
    [switch]$Stop,
    [int]$Port = 8089
)
$ErrorActionPreference = "Stop"
$logs = Join-Path $env:LOCALAPPDATA "ghosttab"
New-Item -ItemType Directory -Force $logs | Out-Null

function Get-Ghosttab {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -match '-m ghosttab' }
}

function Get-Server {
    Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" |
        Where-Object { $_.CommandLine -match "--port $Port( |$)" }
}

function Test-Server {
    try { (Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 2).status -eq "ok" } catch { $false }
}

if ($Stop) {
    Get-Ghosttab | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Get-Server | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    "ghosttab stopped"
    return
}

if (-not (Test-Server)) {
    if (-not (Get-Server)) {
        & (Join-Path $PSScriptRoot "start-server.ps1") -Port $Port -Background
    }
    # The first start downloads the model, which can take several minutes.
    $deadline = (Get-Date).AddMinutes(20)
    while (-not (Test-Server)) {
        if ((Get-Date) -gt $deadline -or -not (Get-Server)) {
            throw "llama-server did not start; see $logs\llama-server.err.log"
        }
        Start-Sleep -Seconds 2
    }
}

if (Get-Ghosttab) {
    "ghosttab is already running"
    return
}
$log = Join-Path $logs "ghosttab.log"
if (Test-Path $log) { Move-Item $log "$log.1" -Force }
Start-Process -FilePath (Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe") `
    -ArgumentList "-m", "ghosttab", "--log-file", "`"$log`"" -WorkingDirectory $PSScriptRoot
"ghosttab started; log: $log"
