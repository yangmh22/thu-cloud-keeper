param(
  [string]$Destination,
  [string]$TaskName = "THUCloudKeeperDailySync",
  [string]$At = "04:00",
  [int]$Workers = 4,
  [string]$PythonExe = "",
  [switch]$DryRun,
  [switch]$RunNow
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Quote-TaskArgument {
  param([string]$Value)
  return '"' + ($Value -replace '"', '\"') + '"'
}

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$RunnerScript = Join-Path $ProjectRoot "scripts\run_scheduled_sync.ps1"

if (-not (Test-Path -LiteralPath $RunnerScript)) {
  throw "Scheduled sync runner not found: $RunnerScript"
}

if (-not $Destination) {
  $BackupFolder = -join ([char[]](0x6E05, 0x534E, 0x4E91, 0x76D8, 0x5907, 0x4EFD))
  $Destination = Join-Path "D:\Fbackup" $BackupFolder
}
$Destination = [System.IO.Path]::GetFullPath($Destination)
New-Item -ItemType Directory -Force -Path $Destination | Out-Null

if (-not $PythonExe) {
  $PythonCommand = Get-Command python -ErrorAction Stop
  $PythonExe = $PythonCommand.Source
}
$PythonExe = [System.IO.Path]::GetFullPath($PythonExe)

try {
  $ParsedAt = [TimeSpan]::Parse($At)
} catch {
  throw "Invalid time '$At'. Use HH:mm, for example 04:00."
}
$RunAt = [DateTime]::Today.Add($ParsedAt)

$ActionArgs = @(
  "-NoProfile",
  "-ExecutionPolicy",
  "Bypass",
  "-File",
  $RunnerScript,
  "-Destination",
  $Destination,
  "-Workers",
  [string]$Workers,
  "-PythonExe",
  $PythonExe
)
if ($DryRun) {
  $ActionArgs += "-DryRun"
}

$Action = New-ScheduledTaskAction `
  -Execute "powershell.exe" `
  -Argument (($ActionArgs | ForEach-Object { Quote-TaskArgument $_ }) -join " ") `
  -WorkingDirectory $ProjectRoot

$Trigger = New-ScheduledTaskTrigger -Daily -At $RunAt
$Settings = New-ScheduledTaskSettingsSet `
  -MultipleInstances IgnoreNew `
  -StartWhenAvailable `
  -WakeToRun `
  -ExecutionTimeLimit (New-TimeSpan -Hours 12) `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries

$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal `
  -UserId $CurrentUser `
  -LogonType Interactive `
  -RunLevel Limited

Register-ScheduledTask `
  -TaskName $TaskName `
  -Action $Action `
  -Trigger $Trigger `
  -Settings $Settings `
  -Principal $Principal `
  -Description "Run THU Cloud Keeper incremental sync every day." `
  -Force | Out-Null

Write-Host "Registered scheduled task: $TaskName"
Write-Host "Schedule: daily at $At"
Write-Host "Destination: $Destination"
Write-Host "Python: $PythonExe"
Write-Host "Runner: $RunnerScript"
$TaskInfo = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "Next run: $($TaskInfo.NextRunTime)"

if ($RunNow) {
  Start-ScheduledTask -TaskName $TaskName
  Write-Host "Started task once in the background."
}
