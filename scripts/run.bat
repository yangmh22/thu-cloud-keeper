@echo off
set "PROJECT_ROOT=%~dp0.."
set "PYTHONPATH=%PROJECT_ROOT%\src"
python -m tsinghua_cloud_backup.cli %*
