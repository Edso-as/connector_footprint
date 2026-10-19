@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/3] Checking dependencies...
python -m pip install --quiet pyinstaller pymupdf opencv-python-headless numpy
if errorlevel 1 goto :fail

echo [2/3] Building exe (one file, no console)...
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name PDF2Footprint ^
  --exclude-module matplotlib --exclude-module scipy --exclude-module pandas ^
  footprint_wizard.py
if errorlevel 1 goto :fail

echo [3/3] Done: dist\PDF2Footprint.exe
exit /b 0

:fail
echo Build failed.
exit /b 1
