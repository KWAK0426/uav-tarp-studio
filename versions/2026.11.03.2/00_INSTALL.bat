@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher.ps1" -Action Setup %*
set "RESULT=%ERRORLEVEL%"
if not defined ELC_STUDIO_NO_PAUSE pause
exit /b %RESULT%
