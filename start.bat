@echo off
rem Double-click to start Tax Automaton. Same as start.ps1; passes on any options (e.g. -Lan).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 pause
