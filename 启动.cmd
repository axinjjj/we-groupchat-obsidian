@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launchers\启动.ps1" %*
exit /b %ERRORLEVEL%
