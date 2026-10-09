@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Setup IP-MAC Image Redactor

echo ============================================================
echo   IP/MAC Image Redactor - Check and Setup
echo ============================================================
echo.

set "SCRIPT_DIR=%~dp0"
set "SCRIPT=%SCRIPT_DIR%redact_network_ids.py"
set "PYTHON_CMD="

if not exist "%SCRIPT%" (
  echo [ERROR] redact_network_ids.py is missing from:
  echo         %SCRIPT_DIR%
  goto :failed
)

rem Find Python via the Windows Python launcher or PATH.
where py >nul 2>nul
if not errorlevel 1 (
  py -3 --version >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=py -3"
)
if not defined PYTHON_CMD (
  where python >nul 2>nul
  if not errorlevel 1 (
    python --version >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python"
  )
)
if not defined PYTHON_CMD (
  echo [ERROR] Python 3 was not found. Install Python 3.10+ and enable "Add Python to PATH".
  echo         https://www.python.org/downloads/windows/
  goto :failed
)
echo [OK] Python found: %PYTHON_CMD%
%PYTHON_CMD% --version

%PYTHON_CMD% -m pip --version >nul 2>nul
if errorlevel 1 (
  echo [INFO] Bootstrapping pip...
  %PYTHON_CMD% -m ensurepip --upgrade
  if errorlevel 1 goto :pip_failed
)

echo.
echo [INFO] Checking/installing Python packages Pillow and pytesseract...
%PYTHON_CMD% -m pip install --user --upgrade Pillow pytesseract
if errorlevel 1 goto :pip_failed
echo [OK] Python packages are ready.

echo.
set "TESSERACT_CMD="
where tesseract >nul 2>nul
if not errorlevel 1 (
  for /f "delims=" %%I in ('where tesseract 2^>nul') do if not defined TESSERACT_CMD set "TESSERACT_CMD=%%I"
)
if not defined TESSERACT_CMD if exist "%ProgramFiles%\Tesseract-OCR\tesseract.exe" set "TESSERACT_CMD=%ProgramFiles%\Tesseract-OCR\tesseract.exe"
if not defined TESSERACT_CMD if exist "%ProgramFiles(x86)%\Tesseract-OCR\tesseract.exe" set "TESSERACT_CMD=%ProgramFiles(x86)%\Tesseract-OCR\tesseract.exe"
if not defined TESSERACT_CMD (
  echo [MISSING] Tesseract OCR was not found.
  echo           Install it from https://github.com/UB-Mannheim/tesseract/wiki
  echo           Then rerun this batch file. During installation, include English language data.
  goto :failed
)
echo [OK] Tesseract found: %TESSERACT_CMD%
"%TESSERACT_CMD%" --version

set "IMAGEMAGICK_CMD="
where magick >nul 2>nul
if not errorlevel 1 (
  for /f "delims=" %%I in ('where magick 2^>nul') do if not defined IMAGEMAGICK_CMD set "IMAGEMAGICK_CMD=%%I"
)
if not defined IMAGEMAGICK_CMD (
  echo [MISSING] ImageMagick was not found.
  echo           Install it from https://imagemagick.org/script/download.php#windows
  echo           Enable adding ImageMagick to PATH, then rerun this batch file.
  goto :failed
)
echo [OK] ImageMagick found: %IMAGEMAGICK_CMD%
"%IMAGEMAGICK_CMD%" -version

echo.
echo [SUCCESS] Dependencies are ready. Starting the redactor GUI...
echo.
if defined TESSERACT_CMD (
  %PYTHON_CMD% "%SCRIPT%" --tesseract-cmd "%TESSERACT_CMD%"
) else (
  %PYTHON_CMD% "%SCRIPT%"
)
set "APP_EXIT=%ERRORLEVEL%"
if not "%APP_EXIT%"=="0" (
  echo.
  echo [ERROR] The program exited with code %APP_EXIT%.
  goto :failed
)
echo.
echo [DONE] Processing finished.
goto :finish

:pip_failed
echo [ERROR] Could not install/check Python packages. Check your internet connection and permissions.
goto :failed

:failed
echo.
echo Setup or processing did not complete. Fix the message above and rerun this file.

:finish
echo.
pause
endlocal

