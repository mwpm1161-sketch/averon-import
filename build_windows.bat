@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title Averon Import Build

if not exist ".venv\Scripts\python.exe" goto :missing
set "VENV_PY=.venv\Scripts\python.exe"

"%VENV_PY%" -m pip install pyinstaller
if errorlevel 1 goto :failed

"%VENV_PY%" -m PyInstaller --noconfirm --clean --onedir --name "Averon Import" ^
  --add-data "averon_import\templates;averon_import\templates" ^
  --add-data "averon_import\static;averon_import\static" ^
  --add-data "averon_import\models;averon_import\models" ^
  --collect-all pytesseract ^
  --collect-all fitz ^
  run.py
if errorlevel 1 goto :failed

echo.
echo Build created in: dist\Averon Import\
echo Tesseract OCR must be installed separately on the target computer.
pause
exit /b 0

:missing
echo Run setup_windows.bat before building.
pause
exit /b 1

:failed
echo Windows build failed.
pause
exit /b 1
