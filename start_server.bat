@echo off
chcp 65001 > nul
echo ========================================================
echo   ⚡ NinePlus Platform Server & Web 调试控制台 启动脚本
echo ========================================================
echo.

echo 1. 正在检查并自动安装依赖包 (ninecli, fastapi, uvicorn)...
pip install -r requirements.txt

echo.
echo 2. 正在启动 NinePlus 后端 RESTful API 服务...
echo 网页调试控制台地址: http://localhost:8888
echo.
python server.py
pause
