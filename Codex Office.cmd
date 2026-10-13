@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_codex_office.ps1"
if errorlevel 1 pause
