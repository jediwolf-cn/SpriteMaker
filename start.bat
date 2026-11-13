@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   SpriteMaker - 2D 游戏精灵动画制作
echo ============================================
echo.
python -c "import PIL" 2>nul || (
  echo [提示] 缺少 Pillow，正在安装...
  python -m pip install Pillow
)
echo.
echo 启动后浏览器打开 http://127.0.0.1:8787
echo 按 Ctrl+C 停止
echo.
python main.py web
pause
