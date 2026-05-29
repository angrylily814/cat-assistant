#!/bin/bash
# 猫咪私人助理 启动脚本

# 修复 Anaconda 的 sqlite3 DLL 问题
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="/d/Anaconda3/Library/bin:$PATH"

echo "🐱 正在启动猫咪私人助理..."
cd "$SCRIPT_DIR"
python app.py
