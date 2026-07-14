@echo off
cd /d "%~dp0"
if exist "python\python.exe" (
    python\python.exe rss_local.py
) else (
    python rss_local.py
)
pause