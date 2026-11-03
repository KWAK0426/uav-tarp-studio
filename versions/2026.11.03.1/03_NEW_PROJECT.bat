@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher.ps1" -Action New %*
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" if not defined ELC_STUDIO_NO_PAUSE pause
exit /b %RESULT%
