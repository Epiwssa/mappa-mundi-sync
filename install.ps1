# Links the plugin into your QGIS profile so edits here are picked up on QGIS restart.
#   powershell -ExecutionPolicy Bypass -File qgis-plugin\install.ps1 [-Profile default]
param([string]$Profile = "default")

$plugins = Join-Path $env:APPDATA "QGIS\QGIS3\profiles\$Profile\python\plugins"
New-Item -ItemType Directory -Force $plugins | Out-Null
$target = Join-Path $plugins "mappa_mundi_sync"
$source = Join-Path $PSScriptRoot "mappa_mundi_sync"

if (Test-Path $target) {
    Write-Host "Already installed at $target"
} else {
    # A junction needs no admin rights and keeps QGIS pointing at this folder.
    New-Item -ItemType Junction -Path $target -Target $source | Out-Null
    Write-Host "Linked $target -> $source"
}
Write-Host "Restart QGIS, then enable 'Mappa Mundi Sync' in Plugins > Manage and Install Plugins > Installed."
