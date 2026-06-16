@echo off
REM issue_monitor auto-start launcher (Startup entry)
REM Runs start_monitor.ps1 with ExecutionPolicy bypass; launcher window hidden.
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\Users\user\Desktop\issue_monitor\start_monitor.ps1"
