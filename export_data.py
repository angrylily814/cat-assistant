"""
导出猫助理数据库到 JSON 文件，用于云端迁移。

用法: python export_data.py
输出: exported_data.json
"""
import os, sys

# Windows Anaconda sqlite3 DLL 修复
_conda_bin = os.path.join(os.path.dirname(sys.executable), 'Library', 'bin')
if os.path.isdir(_conda_bin):
    if hasattr(os, 'add_dll_directory'):
        os.add_dll_directory(_conda_bin)
    os.environ['PATH'] = _conda_bin + os.pathsep + os.environ.get('PATH', '')

import json, sqlite3, io
from pathlib import Path

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "cat_assistant.db"
EXPORT_FILE = BASE_DIR / "exported_data.json"

# 需要导出的表（按依赖顺序，排除 sqlite_sequence 系统表）
TABLE_ORDER = [
    "cat_info",
    "context_settings",
    "events",
    "expenses",
    "inventory",
    "photos",
    "toys",
    "weight_records",
]

# 导出时不包含自增 ID 的表（用其他列做主键）
NON_ID_PK_MAP = {
    "context_settings": ["key"],
}

def export():
    if not DB_PATH.exists():
        print(f"❌ 数据库文件不存在: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    data = {}
    total = 0

    for table in TABLE_ORDER:
        rows = conn.execute(f"SELECT * FROM '{table}'").fetchall()
        table_data = [dict(r) for r in rows]
        data[table] = table_data
        total += len(table_data)

    conn.close()

    with open(EXPORT_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    print(f"✅ 导出完成: {EXPORT_FILE}")
    print(f"   共导出 {total} 条记录")
    print(f"   {', '.join(f'{t}={len(data[t])}' for t in TABLE_ORDER)}")


if __name__ == "__main__":
    export()
