# Scheduled Sync

THU Cloud Keeper can run as a daily unattended incremental sync on Windows.

## Sync Model

The scheduled job scans the cloud-side file list each time, compares every remote file with the local backup path, and downloads only files that are missing or stale locally.

A local file is considered current when:

- the local file exists,
- the local size matches the remote size,
- and, when the remote API provides an `mtime`, the local modified time matches that cloud `mtime` within a small filesystem tolerance.

This is file-level incremental sync. It is similar in spirit to `rsync`, but it is not block-level delta sync. If a large remote file changes, the whole changed file is downloaded again.

The scheduled job does not delete local files that disappeared from the cloud. This is intentional for backup safety: cloud deletion, sharing permission changes, or account-side mistakes should not silently remove existing local backup copies.

## Store Token

Store the token in Windows Credential Manager:

```powershell
$env:TSINGHUA_CLOUD_TOKEN = "paste-token-here"
python -m tsinghua_cloud_backup.cli store-token
Remove-Item Env:\TSINGHUA_CLOUD_TOKEN
```

The credential target is:

```text
THUCloudKeeper:TsinghuaCloudToken
```

## Install Daily 04:00 Task

```powershell
.\scripts\install_scheduled_sync.ps1 -Force
```

Default task name:

```text
THU Cloud Keeper Daily Sync
```

Default destination:

```text
D:\Fbackup\清华云盘备份
```

Default schedule:

```text
Every day at 04:00
After current user logon, with a short random delay
```

The task is configured with `StartWhenAvailable` and `WakeToRun`, so Windows can run it after a missed scheduled time and may wake the computer when the system allows wake timers.

To disable the logon trigger:

```powershell
.\scripts\install_scheduled_sync.ps1 -NoLogonTrigger -Force
```

## Run Manually

```powershell
.\scripts\run_scheduled_sync.ps1
```

## Dry Run

To scan and compare without downloading files:

```powershell
python -m tsinghua_cloud_backup.cli sync --destination "D:\Fbackup\清华云盘备份" --all-categories --dry-run
```

## Logs

Each scheduled run writes a timestamped transcript log under:

```text
D:\Fbackup\清华云盘备份\_backup_metadata\scheduled_logs\
```

The normal backup metadata is also refreshed in:

```text
D:\Fbackup\清华云盘备份\_backup_metadata\
```

If a scheduled run fails, the PowerShell wrapper shows a Windows error dialog with the failure message and the exact transcript log path. For non-interactive runs or tests, pass `-NoPopup` to suppress the dialog:

```powershell
.\scripts\run_scheduled_sync.ps1 -NoPopup
```

## Check Task Status

```powershell
Get-ScheduledTask -TaskName "THU Cloud Keeper Daily Sync"
Get-ScheduledTaskInfo -TaskName "THU Cloud Keeper Daily Sync"
```
