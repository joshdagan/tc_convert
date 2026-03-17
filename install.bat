@echo off
echo.
echo   AFL Timecode Converter - Setup
echo   ================================
echo.
echo   Step 1 of 4: Installing Python...
winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements
echo.
echo   Step 2 of 4: Installing FFmpeg...
winget install Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
echo.
echo   Step 3 of 4: Installing Tesseract OCR...
winget install UB-Mannheim.TesseractOCR --accept-package-agreements --accept-source-agreements
echo.
echo   Step 4 of 4: Installing Python packages...
echo   If you see "pip is not recognized", close this window and run install.bat again.
echo.
pip install Pillow pytesseract
echo.
echo   ================================
echo   Setup complete!
echo   Double-click run.bat to launch.
echo   ================================
echo.
pause
