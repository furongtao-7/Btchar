@echo off
chcp 65001 >nul
"%~dp0.venv\Scripts\python.exe" "%~dp0v2t.py" %*
