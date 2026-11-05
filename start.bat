@echo off
setlocal
cd /d "%~dp0"

rem ============================================================
rem  This launcher is intentionally ASCII-only.
rem  cmd.exe reads .bat using the OEM codepage (GBK/936 on zh-CN
rem  Windows). Non-ASCII bytes would be mis-parsed and break the
rem  script. All Chinese UI text is printed by Python instead.
rem ============================================================

rem 1) installed build: bundled runtime (has every dependency)
set "PYTHON=%~dp0runtime\python.exe"
if exist "%PYTHON%" goto :run

rem 2) source checkout on the build machine
set "PYTHON=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if exist "%PYTHON%" goto :run

rem 3) whatever Python the host has
set "PYTHON=py"
where py >nul 2>nul
if not errorlevel 1 goto :run

set "PYTHON=python"
where python >nul 2>nul
if not errorlevel 1 goto :run

echo.
echo  [ERROR] Python interpreter not found.
echo.
echo  Run the installer again, or edit the PYTHON path in this file.
echo  Looked for:
echo    %~dp0runtime\python.exe
echo.
pause
exit /b 1

:run
"%PYTHON%" run.py --menu
echo.
pause
exit /b 0
