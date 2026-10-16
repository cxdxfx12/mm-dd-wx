@echo off
setlocal
cd /d "%~dp0"

rem ============================================================
rem  Launch the GUI (no console window).
rem  It needs a Python that ships tkinter (the official installer
rem  includes it by default). app/gui.py re-launches itself with a
rem  suitable interpreter automatically if the first pick lacks it.
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

start "" "%PYW%" "%~dp0app\gui.py"
exit /b 0
