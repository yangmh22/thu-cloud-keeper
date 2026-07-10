param(
  [string]$Destination,
  [int]$Workers = 4,
  [string]$PythonExe = "python",
  [string]$ProjectRoot = "",
  [switch]$DryRun,
  [switch]$NoPopup
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Show-SyncFailurePopup {
  param(
    [string]$Message,
    [string]$LogPath
  )

  try {
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
    $Body = @(
      "THU Cloud Keeper scheduled sync failed.",
      "",
      $Message,
      "",
      "Log file:",
      $LogPath
    ) -join [Environment]::NewLine

    [System.Windows.Forms.MessageBox]::Show(
      $Body,
      "THU Cloud Keeper sync failed",
      [System.Windows.Forms.MessageBoxButtons]::OK,
      [System.Windows.Forms.MessageBoxIcon]::Error
    ) | Out-Null
  } catch {
    Write-Warning "Failed to show popup notification: $($_.Exception.Message)"
  }
}

if (-not $ProjectRoot) {
  $ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path

if (-not $Destination) {
  $BackupFolder = -join ([char[]](0x6E05, 0x534E, 0x4E91, 0x76D8, 0x5907, 0x4EFD))
  $Destination = Join-Path "D:\Fbackup" $BackupFolder
}
$Destination = [System.IO.Path]::GetFullPath($Destination)

$MetadataDir = Join-Path $Destination "_backup_metadata"
$LogDir = Join-Path $MetadataDir "scheduled_logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$TranscriptPath = Join-Path $LogDir "scheduled-sync-$Stamp.log"

$PreviousPythonPath = $env:PYTHONPATH
if ($PreviousPythonPath) {
  $env:PYTHONPATH = "$ProjectRoot\src$([System.IO.Path]::PathSeparator)$PreviousPythonPath"
} else {
  $env:PYTHONPATH = "$ProjectRoot\src"
}

$ExitCode = 0
$PopupMessage = ""
Start-Transcript -Path $TranscriptPath -Append | Out-Null
try {
  Write-Host "THU Cloud Keeper scheduled sync"
  Write-Host "Started:     $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
  Write-Host "ProjectRoot: $ProjectRoot"
  Write-Host "Destination: $Destination"
  Write-Host "Python:      $PythonExe"
  Write-Host "Workers:     $Workers"
  if ($DryRun) {
    Write-Host "Mode:        dry-run"
  }

  $CliArgs = @(
    "-m",
    "tsinghua_cloud_backup.cli",
    "sync",
    "--destination",
    $Destination,
    "--all-categories",
    "--workers",
    [string]$Workers,
    "--progress-interval",
    "300"
  )
  if ($DryRun) {
    $CliArgs += "--dry-run"
  }

  & $PythonExe @CliArgs
  $ExitCode = $LASTEXITCODE
  if ($null -eq $ExitCode) {
    $ExitCode = 0
  }
  if ($ExitCode -ne 0) {
    throw "Sync command exited with code $ExitCode."
  }

  Write-Host "Finished:    $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
} catch {
  $ExitCode = 1
  $ErrorMessage = $_.Exception.Message
  $PopupMessage = $ErrorMessage
  Write-Error -ErrorRecord $_ -ErrorAction Continue
} finally {
  $env:PYTHONPATH = $PreviousPythonPath
  Stop-Transcript | Out-Null
}

if (($ExitCode -ne 0) -and (-not $NoPopup)) {
  Show-SyncFailurePopup -Message $PopupMessage -LogPath $TranscriptPath
}

exit $ExitCode
