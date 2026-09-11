@echo off
rem CodeKit Plugin uninstall entry point for CMD and double-click users.
rem Delegates to uninstall.ps1; pass the same arguments through unchanged.
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %*
exit /b %ERRORLEVEL%
