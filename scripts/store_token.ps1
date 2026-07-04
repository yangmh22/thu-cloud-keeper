param(
  [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path

$PreviousPythonPath = $env:PYTHONPATH
if ($PreviousPythonPath) {
  $env:PYTHONPATH = "$ProjectRoot\src$([System.IO.Path]::PathSeparator)$PreviousPythonPath"
} else {
  $env:PYTHONPATH = "$ProjectRoot\src"
}

$SecureToken = Read-Host "Tsinghua Cloud token" -AsSecureString
$TokenPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureToken)
try {
  $Token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($TokenPtr)
  if (-not $Token) {
    throw "Token is empty."
  }
  $Token | & $PythonExe -m tsinghua_cloud_backup.cli store-token
  if ($LASTEXITCODE -ne 0) {
    throw "Token storage command failed with exit code $LASTEXITCODE."
  }
} finally {
  if ($TokenPtr -ne [IntPtr]::Zero) {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($TokenPtr)
  }
  $Token = $null
  $env:PYTHONPATH = $PreviousPythonPath
}
