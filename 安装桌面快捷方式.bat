@echo off
setlocal
cd /d "%~dp0"

rem ============================================================
rem  One-click installer: desktop + start-menu shortcuts.
rem  Runs app/setup.py with pythonw (no console window); the
rem  script reports the result in a message box.
rem ============================================================

set "PYW="
for %%D in (
  "%LOCALAPPDATA%\Programs\Python\Python312"
  "%LOCALAPPDATA%\Programs\Python\Python313"
  "%LOCALAPPDATA%\Programs\Python\Python311"
  "%LOCALAPPDATA%\Programs\Python\Python310"
  "%LOCALAPPDATA%\Python\pythoncore-3.14-64"
  "%ProgramFiles%\Python312"
  "%ProgramFiles%\Python311"
) do (
  if not defined PYW if exist "%%~D\pythonw.exe" set "PYW=%%~D\pythonw.exe"
)
if not defined PYW set "PYW=pythonw.exe"

"%PYW%" "%~dp0app\setup.py"
exit /b 0
