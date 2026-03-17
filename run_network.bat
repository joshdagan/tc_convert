@echo off
start "AFL TC Converter (Network)" python "%~dp0tc_converter.py" --network
timeout /t 2 /nobreak >nul
start http://localhost:8765
