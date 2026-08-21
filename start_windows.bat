@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title Averon Import

set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
set "RUN_FILE=%CD%\run.py"

if not exist "%PYTHON_EXE%" goto :missing_python
if not exist "%RUN_FILE%" goto :missing_run

"%PYTHON_EXE%" "%RUN_FILE%"
set "APP_EXIT=%ERRORLEVEL%"
if "%APP_EXIT%"=="0" goto :success

echo.
echo ERROR: Averon Import stopped with code %APP_EXIT%.
echo Check the console output above for details.
pause
exit /b %APP_EXIT%

:missing_python
echo.
echo ERROR: Averon Import is not installed in this folder.
echo Run setup_windows.bat first.
pause
exit /b 1

:missing_run
echo.
echo ERROR: run.py was not found.
echo Extract the complete Averon Import archive into a new folder.
pause
exit /b 1

:success
exit /b 0
