@echo off
rem CodeKit Plugin installer entry point for CMD and double-click users.
rem Delegates to install.ps1; pass the same arguments through unchanged.
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
exit /b %ERRORLEVEL%
