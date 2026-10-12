# Install ghosttab for the current user: creates the virtual environment if
# needed, starts ghosttab at every login (a shortcut in the Startup folder)
# and starts it now. uninstall.ps1 reverses this.
$ErrorActionPreference = "Stop"
$venv = Join-Path $PSScriptRoot ".venv"
if (-not (Test-Path (Join-Path $venv "Scripts\pythonw.exe"))) {
    python -m venv $venv
    & (Join-Path $venv "Scripts\python.exe") -m pip install --quiet -r (Join-Path $PSScriptRoot "requirements.txt")
}

$shortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "ghosttab.lnk"
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($shortcut)
$link.TargetPath = (Get-Command powershell.exe).Source
$link.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSScriptRoot\ghosttab.ps1`""
$link.WorkingDirectory = $PSScriptRoot
$link.WindowStyle = 7
$link.Description = "ghosttab: autocomplete, dictation and selection assistant"
$link.Save()
"Starts at login: $shortcut"

& (Join-Path $PSScriptRoot "ghosttab.ps1")
