# Stop ghosttab and remove it from the Startup folder. The virtual environment,
# the downloaded model and the logs in %LOCALAPPDATA%\ghosttab are kept.
$shortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "ghosttab.lnk"
if (Test-Path $shortcut) {
    Remove-Item $shortcut
    "Removed $shortcut"
}
& (Join-Path $PSScriptRoot "ghosttab.ps1") -Stop
