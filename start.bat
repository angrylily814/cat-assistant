@echo off
chcp 65001 >nul
title 猫咪私人助理

:: 修复 Anaconda sqlite3 DLL
set PATH=D:\Anaconda3\Library\bin;%PATH%

:: 如果在同目录下有 api_key.txt，自动读取
if exist "%~dp0api_key.txt" (
    for /f "usebackq delims=" %%i in ("%~dp0api_key.txt") do set DEEPSEEK_API_KEY=%%i
)

if "%DEEPSEEK_API_KEY%"=="" (
    echo ========================================
    echo   ⚠️  未检测到 DeepSeek API Key！
    echo   请设置环境变量或创建 api_key.txt
    echo ========================================
    echo.
)

:: 关闭占用 8001 端口的旧进程
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8001.*LISTENING" 2^>nul') do (
    echo   已关闭旧进程 PID=%%a
    taskkill /F /PID %%a >nul 2>&1
)

echo 🐱 正在启动猫咪私人助理...
echo.
echo   本机访问: http://localhost:8001
echo   手机访问: http://你的电脑IP:8001
echo.
echo 按 Ctrl+C 停止服务
echo ========================================
echo.

cd /d "%~dp0"
python app.py

pause
