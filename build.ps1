param(
    [string]$OutputDirectory = 'gui-dist',
    [string]$WorkDirectory = 'gui-build'
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$distPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot $OutputDirectory))
$workPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot $WorkDirectory))
$workspacePrefix = $PSScriptRoot.TrimEnd('\') + '\'
if (-not $distPath.StartsWith($workspacePrefix, [System.StringComparison]::OrdinalIgnoreCase) -or -not $workPath.StartsWith($workspacePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Build output and work directories must stay inside the project.'
}
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Create .venv and install requirements-gui.txt before building.'
}
New-Item -ItemType Directory -Force -Path 'gui-build' | Out-Null
New-Item -ItemType Directory -Force -Path $workPath | Out-Null
$env:PYINSTALLER_CONFIG_DIR = Join-Path $workPath 'cache'
if (Test-Path -LiteralPath (Join-Path $distPath 'AnalyticsBot\user_data')) {
    throw 'The output folder contains user_data. Choose another OutputDirectory or back up user_data first.'
}
& $python build_icon.py
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to create the application icon.'
}
$iconPath = Join-Path $PSScriptRoot 'gui-build\analyticsbot.ico'
& $python -m PyInstaller --noconfirm --clean --onedir --windowed --name AnalyticsBot --icon $iconPath --distpath $distPath --workpath $workPath --specpath $workPath --collect-data googleapiclient --collect-data tzdata gui_entry.py
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'WINDOWS_README.txt') -Destination (Join-Path $distPath 'AnalyticsBot\README.txt') -Force
Write-Host "Ready: $distPath\AnalyticsBot\AnalyticsBot.exe"
