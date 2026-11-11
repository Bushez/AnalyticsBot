$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Create .venv and install requirements-gui.txt before building.'
}
New-Item -ItemType Directory -Force -Path 'gui-build' | Out-Null
$env:PYINSTALLER_CONFIG_DIR = Join-Path $PSScriptRoot 'gui-build\cache'
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'gui-dist\AnalyticsBot\user_data')) {
    throw 'Move gui-dist\AnalyticsBot\user_data to a safe location before rebuilding.'
}
& $python build_icon.py
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to create the application icon.'
}
$iconPath = Join-Path $PSScriptRoot 'gui-build\analyticsbot.ico'
& $python -m PyInstaller --noconfirm --clean --onedir --windowed --name AnalyticsBot --icon $iconPath --distpath gui-dist --workpath gui-build --specpath gui-build --collect-data googleapiclient --collect-data tzdata gui_entry.py
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'WINDOWS_README.txt') -Destination (Join-Path $PSScriptRoot 'gui-dist\AnalyticsBot\README.txt') -Force
Write-Host 'Ready: gui-dist\AnalyticsBot\AnalyticsBot.exe'
