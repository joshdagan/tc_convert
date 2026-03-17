# AFL Timecode Converter - One-time setup
# Right-click this file and select "Run with PowerShell"

Write-Host ""
Write-Host "  AFL Timecode Converter - Setup" -ForegroundColor Cyan
Write-Host "  ================================" -ForegroundColor Cyan
Write-Host ""

# ── Python ────────────────────────────────────────────────────────────────────
if (Get-Command python -ErrorAction SilentlyContinue) {
    Write-Host "  [OK] Python already installed" -ForegroundColor Green
} else {
    Write-Host "  Installing Python..." -ForegroundColor Yellow
    winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements
}

# ── FFmpeg ────────────────────────────────────────────────────────────────────
if (Get-Command ffprobe -ErrorAction SilentlyContinue) {
    Write-Host "  [OK] FFmpeg already installed" -ForegroundColor Green
} else {
    Write-Host "  Installing FFmpeg..." -ForegroundColor Yellow
    winget install Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
}

# ── Tesseract ─────────────────────────────────────────────────────────────────
$tesseract = "C:\Program Files\Tesseract-OCR\tesseract.exe"
if (Test-Path $tesseract) {
    Write-Host "  [OK] Tesseract already installed" -ForegroundColor Green
} else {
    Write-Host "  Installing Tesseract OCR..." -ForegroundColor Yellow
    winget install UB-Mannheim.TesseractOCR --accept-package-agreements --accept-source-agreements
}

# ── Refresh PATH so pip can find the new Python install ───────────────────────
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("Path","User")

# ── Python packages ───────────────────────────────────────────────────────────
Write-Host "  Installing Python packages..." -ForegroundColor Yellow
python -m pip install Pillow pytesseract --quiet

Write-Host ""
Write-Host "  Setup complete!" -ForegroundColor Green
Write-Host "  Double-click run.bat to launch the app." -ForegroundColor Green
Write-Host ""
Read-Host "  Press Enter to close"
