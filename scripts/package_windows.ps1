$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$AppName = -join ([char[]](0x6E05, 0x534E, 0x4E91, 0x76D8, 0x81EA, 0x52A9, 0x5907, 0x4EFD))

Set-Location $ProjectRoot

python -m pip install --upgrade pyinstaller

python -m PyInstaller `
  --noconfirm `
  --clean `
  --noconsole `
  --name $AppName `
  --distpath "$ProjectRoot\dist" `
  --workpath "$ProjectRoot\build" `
  --specpath "$ProjectRoot\build" `
  --paths "$ProjectRoot\src" `
  --hidden-import tkinter `
  --hidden-import tkinter.ttk `
  "$ProjectRoot\scripts\pyinstaller_entry.py"

$ZipPath = Join-Path $ProjectRoot "dist\$AppName-windows.zip"
if (Test-Path -LiteralPath $ZipPath) {
  Remove-Item -LiteralPath $ZipPath -Force
}
Compress-Archive -LiteralPath "$ProjectRoot\dist\$AppName" -DestinationPath $ZipPath

Write-Host ""
Write-Host "Windows app folder:"
Write-Host "$ProjectRoot\dist\$AppName"
Write-Host ""
Write-Host "Windows zip package:"
Write-Host $ZipPath
