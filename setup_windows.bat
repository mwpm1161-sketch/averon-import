@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title Averon Import Setup

set "LOG_FILE=%CD%\setup_log.txt"
>"%LOG_FILE%" echo Averon Import setup log

 echo ============================================================
 echo Averon Import - Windows setup
 echo ============================================================
 echo.
 echo [1/6] Checking Python 3.11 or newer...

set "PYTHON_CMD="

rem Prefer the normal python command because it points to the runtime
rem that the user has just installed. Fall back to the Python launcher.
where python >nul 2>&1
if not errorlevel 1 (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
        if not errorlevel 1 set "PYTHON_CMD=py -3"
    )
)

if not defined PYTHON_CMD goto :python_missing

>>"%LOG_FILE%" echo Selected command: %PYTHON_CMD%
%PYTHON_CMD% --version >>"%LOG_FILE%" 2>&1
if errorlevel 1 goto :python_missing

echo       Python found: %PYTHON_CMD%

echo [2/6] Creating the virtual environment...
if exist ".venv" if not exist ".venv\Scripts\python.exe" (
    echo       Removing an incomplete .venv folder...
    rmdir /s /q ".venv" >>"%LOG_FILE%" 2>&1
    if exist ".venv" goto :venv_error
)

if not exist ".venv\Scripts\python.exe" (
    %PYTHON_CMD% -m venv ".venv" >>"%LOG_FILE%" 2>&1
    if errorlevel 1 goto :venv_error
) else (
    echo       Existing environment will be reused.
)

set "VENV_PY=.venv\Scripts\python.exe"
if not exist "%VENV_PY%" goto :venv_error

"%VENV_PY%" --version >>"%LOG_FILE%" 2>&1
if errorlevel 1 goto :venv_error

echo [3/6] Updating installer tools...
"%VENV_PY%" -m pip install --upgrade pip setuptools wheel >>"%LOG_FILE%" 2>&1
if errorlevel 1 goto :pip_error

echo [4/6] Installing Averon Import dependencies...
"%VENV_PY%" -m pip install -r requirements.txt >>"%LOG_FILE%" 2>&1
if errorlevel 1 goto :dependencies_error

echo [5/6] Checking OCR...
"%VENV_PY%" scripts\check_environment.py >>"%LOG_FILE%" 2>&1
if errorlevel 1 goto :ocr_warning

echo       OCR is ready.

echo [6/6] Installing accurate engineering OCR models...
powershell -NoProfile -ExecutionPolicy Bypass -File "%CD%\scripts\install_accurate_ocr_models.ps1" >>"%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo       Accurate models were not installed. Standard OCR remains available.
) else (
    echo       Accurate models are ready.
)
goto :success

:ocr_warning
echo.
echo WARNING: Application libraries are installed, but Tesseract OCR
echo with the Russian language package was not detected.
echo Install Tesseract OCR 5 into the default folder:
echo C:\Program Files\Tesseract-OCR\
echo Then run setup_windows.bat again or start the application.
echo [6/6] Accurate models skipped until Tesseract is installed.
goto :success

:python_missing
echo.
echo ERROR: Python 3.11 or newer was not found.
echo Install 64-bit Python from python.org and enable "Add Python to PATH".
echo Details are available in setup_log.txt.
goto :failed

:venv_error
echo.
echo ERROR: The Python virtual environment could not be created.
echo The installer automatically removes incomplete .venv folders.
echo Details are available in setup_log.txt.
goto :failed

:pip_error
echo.
echo ERROR: pip could not be updated.
echo Check the internet connection and setup_log.txt.
goto :failed

:dependencies_error
echo.
echo ERROR: Application dependencies could not be installed.
echo Check the internet connection, antivirus restrictions and setup_log.txt.
goto :failed

:success
echo.
echo ============================================================
echo Setup completed.
echo Run start_windows.bat to open Averon Import.
echo Setup log: setup_log.txt
echo ============================================================
pause
exit /b 0

:failed
echo.
echo Setup failed. Log file: setup_log.txt
pause
exit /b 1
