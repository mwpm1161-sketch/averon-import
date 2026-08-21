@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title Averon Import - Accurate OCR Models

echo ============================================================
echo Averon Import - accurate OCR model installer
echo ============================================================
echo.

where powershell >nul 2>&1
if errorlevel 1 goto :missing

powershell -NoProfile -ExecutionPolicy Bypass -File "%CD%\scripts\install_accurate_ocr_models.ps1"
if errorlevel 1 goto :failed

echo.
echo Accurate engineering OCR is ready.
echo Restart Averon Import to enable it.
pause
exit /b 0

:missing
echo ERROR: Windows PowerShell was not found.
pause
exit /b 1

:failed
echo.
echo ERROR: Accurate OCR models could not be downloaded.
echo Check the internet connection and try again.
echo Standard OCR remains available.
pause
exit /b 1
