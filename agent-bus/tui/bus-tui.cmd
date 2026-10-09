@echo off
rem bus-tui launcher for Windows: forwards every argument to the Python launcher next to this file
where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 "%~dp0bus-tui" %*
) else (
  python "%~dp0bus-tui" %*
)
exit /b %ERRORLEVEL%
