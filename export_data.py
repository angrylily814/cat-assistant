import os, sys

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

TABLE_ORDER = [
    "cat_info", "context_settings", "events", "expenses",
    "inventory", "photos", "toys", "weight_records",
]

conn = sqlite3.connect(str(DB_PATH))
conn.row_factory = sqlite3.Row

data = {}
total = 0
for table in TABLE_ORDER:
    rows = conn.execute(f"SELECT * FROM '{table}'").fetchall()
    data[table] = [dict(r) for r in rows]
    total += len(data[table])

conn.close()

with open(EXPORT_FILE, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2, default=str)

print(f"exported_data.json  {total} records  {'  '.join(f'{t}={len(data[t])}' for t in TABLE_ORDER)}")
