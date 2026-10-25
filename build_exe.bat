@echo off
rem 打包 Btchar 为单文件夹exe (输出在 dist\Btchar\)
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" -m pip install -q pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed --name Btchar --icon Btchar.ico --add-data "static;static" --add-data "Btchar.ico;." --hidden-import yt_dlp --hidden-import soundcard --hidden-import av --collect-all faster_whisper --collect-all ctranslate2 --collect-all soundcard --collect-all pywebview --collect-all pythonnet app_native.py
echo.
echo 打包完成: dist\Btchar\Btchar.exe (整个文件夹一起分发)
pause
