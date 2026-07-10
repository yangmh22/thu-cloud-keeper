param(
  [string]$TaskName = "THU Cloud Keeper Daily Sync",
  [string]$Destination = "",
  [string]$At = "04:00",
  [int]$Workers = 4,
  [string]$PythonExe = "D:\ApplicationAndData\MiniConda\python.exe",
  [switch]$NoLogonTrigger,
  [switch]$Force
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$SyncScript = Join-Path $ProjectRoot "scripts\run_scheduled_sync.ps1"

if (-not (Test-Path -LiteralPath $SyncScript)) {
  throw "Scheduled sync script not found: $SyncScript"
}
if (-not (Test-Path -LiteralPath $PythonExe)) {
  throw "Python executable not found: $PythonExe"
}

if (-not $Destination) {
  $BackupFolder = -join ([char[]](0x6E05, 0x534E, 0x4E91, 0x76D8, 0x5907, 0x4EFD))
  $Destination = Join-Path "D:\Fbackup" $BackupFolder
}
$Destination = [System.IO.Path]::GetFullPath($Destination)

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and -not $Force) {
  throw "Task already exists: $TaskName. Re-run with -Force to update it."
}
if ($existing -and $Force) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

New-Item -ItemType Directory -Force -Path $Destination | Out-Null

$argument = @(
  "-NoProfile",
  "-ExecutionPolicy", "Bypass",
  "-File", "`"$SyncScript`"",
  "-Destination", "`"$Destination`"",
  "-Workers", $Workers,
  "-PythonExe", "`"$PythonExe`""
) -join " "

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument -WorkingDirectory $ProjectRoot
$triggers = @(
  New-ScheduledTaskTrigger -Daily -At $At
)
if (-not $NoLogonTrigger) {
  $triggers += New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME -RandomDelay (New-TimeSpan -Minutes 1)
}
$settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -WakeToRun `
  -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Hours 20) `
  -RestartCount 2 `
  -RestartInterval (New-TimeSpan -Minutes 15)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
  -TaskName $TaskName `
  -Action $action `
  -Trigger $triggers `
  -Settings $settings `
  -Principal $principal `
  -Description "Daily incremental sync from Tsinghua Cloud to local backup folder." | Out-Null

Write-Host "Scheduled task installed:"
Write-Host "  Name: $TaskName"
Write-Host "  Time: $At every day"
if (-not $NoLogonTrigger) {
  Write-Host "  Startup: after user logon"
}
Write-Host "  Destination: $Destination"
Write-Host "  Script: $SyncScript"
Write-Host "  Python: $PythonExe"
Write-Host ""
Write-Host "Token is read from Windows Credential Manager target:"
Write-Host "  THUCloudKeeper:TsinghuaCloudToken"
