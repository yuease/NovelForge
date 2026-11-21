@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-novelforge.ps1"
set "NOVEL_FORGE_EXIT=%ERRORLEVEL%"
if not "%NOVEL_FORGE_EXIT%"=="0" pause
exit /b %NOVEL_FORGE_EXIT%
