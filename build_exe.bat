@echo off
rem 打包 Btchar 为单文件夹exe (输出在 dist\Btchar\)
rem 注意: 首次打包前请先用App完整跑一次转换, 确保模型已缓存
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" -m pip install -q pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
".venv\Scripts\python.exe" -m PyInstaller ^
  --noconfirm --clean --windowed --name Btchar ^
  --icon Btchar.ico ^
  --add-data "static;static" ^
  --add-data "Btchar.ico;." ^
  --hidden-import yt_dlp ^
  --collect-all faster_whisper ^
  --collect-all ctranslate2 ^
  app_native.py
echo.
echo 打包完成: dist\Btchar\Btchar.exe (整个文件夹一起分发)
pause
