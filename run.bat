@echo off
start "AFL TC Converter" python "%~dp0tc_converter.py"
timeout /t 2 /nobreak >nul
start http://localhost:8765
