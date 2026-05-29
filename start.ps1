# 猫咪私人助理 PowerShell 启动脚本

$env:PATH = "D:\Anaconda3\Library\bin;$env:PATH"

Write-Host "🔑 当前 DEEPSEEK_API_KEY: $env:DEEPSEEK_API_KEY" -ForegroundColor Yellow
if (-not $env:DEEPSEEK_API_KEY) {
    Write-Host "⚠️  请先设置 API Key: `$env:DEEPSEEK_API_KEY='你的key'`" -ForegroundColor Red
}

Write-Host "🐱 正在启动猫咪私人助理..." -ForegroundColor Cyan
python app.py
