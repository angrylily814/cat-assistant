"""
猫咪私人助理 — 后端服务
FastAPI + SQLite + DeepSeek Function Calling + asyncio 定时任务
"""

import os
import sys

# 修复 Anaconda 环境下 sqlite3 DLL 加载问题
_conda_bin = os.path.join(os.path.dirname(sys.executable), 'Library', 'bin')
if os.path.isdir(_conda_bin):
    if hasattr(os, 'add_dll_directory'):
        os.add_dll_directory(_conda_bin)
    os.environ['PATH'] = _conda_bin + os.pathsep + os.environ.get('PATH', '')

import json
import re
import sqlite3
import asyncio
import random
from datetime import datetime, date, timedelta
from pathlib import Path
from contextlib import contextmanager, asynccontextmanager

import httpx
from typing import List
from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ===================== 配置 =====================
# 修复 Windows GBK 编码下无法打印 emoji 的问题
if sys.platform == 'win32':
    sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

# 数据存储目录（可通过环境变量自定义，默认 data/，兼容旧 D: 盘路径）
_default_data = str(BASE_DIR / "data")
_legacy_data = "D:/cat-assistant-data"
if not os.environ.get("DATA_DIR"):
    from pathlib import Path as _Path
    if _Path(_legacy_data).exists():
        _default_data = _legacy_data
DATA_DIR = Path(os.environ.get("DATA_DIR", _default_data))
DB_PATH = Path(os.environ.get("DB_PATH", str(BASE_DIR / "cat_assistant.db")))
PHOTOS_DIR = DATA_DIR / "photos"
AVATAR_PATH = PHOTOS_DIR / "avatar.jpg"
DATA_DIR.mkdir(parents=True, exist_ok=True)
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

# 兼容旧路径
_old_upload_dir = BASE_DIR / "uploads"
_old_upload_dir.mkdir(exist_ok=True)

# 优先读环境变量，其次读项目目录下的 api_key.txt
_DEFAULT_KEY = "your-api-key-here"
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY == _DEFAULT_KEY:
    _key_file = BASE_DIR / "api_key.txt"
    if _key_file.exists():
        DEEPSEEK_API_KEY = _key_file.read_text(encoding="utf-8").strip()
    else:
        DEEPSEEK_API_KEY = _DEFAULT_KEY
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"

app = FastAPI(title="猫咪私人助理")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/photos", StaticFiles(directory=str(PHOTOS_DIR)), name="photos")

# ===================== 数据库 =====================

@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ===================== 统一花费统计 =====================
# 所有花费总额查询必须经过此函数，确保 Agent 和仪表盘数据一致

def get_expense_total(start_date: str = None, end_date: str = None, category: str = None):
    """统一花费总额查询 — Agent 和仪表盘共用此函数"""
    with get_db() as conn:
        sql = "SELECT COALESCE(SUM(amount), 0) as total FROM expenses WHERE 1=1"
        params = []
        if start_date:
            sql += " AND date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND date <= ?"
            params.append(end_date)
        if category:
            sql += " AND category = ?"
            params.append(category)
        return conn.execute(sql, params).fetchone()["total"]


def get_expense_by_category(start_date: str = None, end_date: str = None):
    """统一分类花费查询"""
    with get_db() as conn:
        sql = "SELECT category, COALESCE(SUM(amount), 0) as total FROM expenses WHERE 1=1"
        params = []
        if start_date:
            sql += " AND date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND date <= ?"
            params.append(end_date)
        sql += " GROUP BY category"
        return conn.execute(sql, params).fetchall()


def get_month_range(period: str):
    """
    统一的月份/日期范围计算 — 所有 Agent 函数和 API 共用。
    返回 (start_date, end_date) 元组，None 表示无下限/无上限。
    period: "本月" | "上月" | "近3个月" | "近半年" | "今年" | "全部"
    """
    today = date.today()
    if period == "全部":
        return None, None
    elif period == "上月":
        if today.month == 1:
            last_month_start = today.replace(year=today.year - 1, month=12, day=1)
        else:
            last_month_start = today.replace(month=today.month - 1, day=1)
        month_end = (last_month_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        return last_month_start.strftime('%Y-%m-%d'), month_end.strftime('%Y-%m-%d')
    elif period == "近3个月":
        return (today - timedelta(days=90)).strftime('%Y-%m-%d'), today.strftime('%Y-%m-%d')
    elif period == "近半年":
        return (today - timedelta(days=180)).strftime('%Y-%m-%d'), today.strftime('%Y-%m-%d')
    elif period == "今年":
        return today.replace(month=1, day=1).strftime('%Y-%m-%d'), today.strftime('%Y-%m-%d')
    else:  # 本月 — 始终使用月份实际最后一天
        month_start = today.replace(day=1)
        if today.month == 12:
            month_end = today.replace(year=today.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            month_end = today.replace(month=today.month + 1, day=1) - timedelta(days=1)
        return month_start.strftime('%Y-%m-%d'), month_end.strftime('%Y-%m-%d')


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                date TEXT NOT NULL,
                next_due_date TEXT,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                category TEXT NOT NULL,
                brand TEXT,
                amount REAL NOT NULL,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS toys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                preference TEXT CHECK(preference IN ('like', 'dislike', 'neutral')),
                last_used_date TEXT,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS weight_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                weight REAL NOT NULL,
                photo_path TEXT,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name TEXT NOT NULL,
                category TEXT NOT NULL,
                quantity REAL NOT NULL,
                threshold REAL NOT NULL,
                unit TEXT NOT NULL,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS context_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS cat_info (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                name TEXT,
                birthday TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                original_name TEXT,
                upload_date TEXT NOT NULL,
                file_size_kb REAL,
                notes TEXT
            );
        """)
    print("✅ 数据库初始化完成")

    # 迁移：为 cat_info 表添加 bio 列（如果不存在）
    try:
        with get_db() as conn:
            conn.execute("ALTER TABLE cat_info ADD COLUMN bio TEXT")
    except:
        pass  # 列已存在

    # 迁移：为 cat_info 表添加 status 列（如果不存在）
    try:
        with get_db() as conn:
            conn.execute("ALTER TABLE cat_info ADD COLUMN status TEXT DEFAULT ''")
    except:
        pass  # 列已存在


# ===================== 工具函数 =====================

def dict_from_row(row):
    if row is None:
        return None
    return dict(row)


def rows_to_list(rows):
    return [dict(r) for r in rows]


# ===================== Function Calling 定义 =====================

FUNCTIONS = [
    {
        "type": "function",
        "function": {
            "name": "add_event",
            "description": "添加猫咪事件记录（疫苗、驱虫、就医、拔牙等），并设置下次提醒日期",
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["vaccine", "deworm", "vet_visit", "dental", "birthday"],
                             "description": "事件类型"},
                    "date": {"type": "string", "description": "发生日期，格式 YYYY-MM-DD"},
                    "next_due_date": {"type": "string", "description": "下次到期日期，格式 YYYY-MM-DD，疫苗默认+365天，驱虫默认+90天"},
                    "notes": {"type": "string", "description": "备注信息"}
                },
                "required": ["type", "date"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_expense",
            "description": "记录猫咪花费",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "日期，默认今天 YYYY-MM-DD"},
                    "category": {"type": "string", "enum": ["猫粮", "玩具", "医疗", "猫砂", "零食", "用品", "其他"],
                                 "description": "花费类别"},
                    "brand": {"type": "string", "description": "品牌名称或玩具名称"},
                    "amount": {"type": "number", "description": "金额（元）"},
                    "notes": {"type": "string", "description": "备注"}
                },
                "required": ["category", "amount"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_toy_preference",
            "description": "记录猫咪对某个玩具的喜好程度",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "玩具名称"},
                    "preference": {"type": "string", "enum": ["like", "dislike", "neutral"],
                                   "description": "喜好程度"},
                    "notes": {"type": "string", "description": "备注"}
                },
                "required": ["name", "preference"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_weight_record",
            "description": "记录猫咪体重",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "日期，默认今天 YYYY-MM-DD"},
                    "weight": {"type": "number", "description": "体重（kg）"},
                    "notes": {"type": "string", "description": "备注"}
                },
                "required": ["weight"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_inventory",
            "description": "更新库存（猫粮、猫砂、药品等）。购买时 quantity 为正数累加，消耗时用 set_quantity 直接设剩余量",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_name": {"type": "string", "description": "物品名称"},
                    "category": {"type": "string", "enum": ["猫粮", "猫砂", "药品", "零食", "用品"],
                                 "description": "物品类别"},
                    "set_quantity": {"type": "number", "description": "设置当前剩余量"},
                    "threshold": {"type": "number", "description": "阈值，低于此值时提醒"},
                    "unit": {"type": "string", "description": "单位，如 kg、L、片、包"},
                    "notes": {"type": "string", "description": "备注"}
                },
                "required": ["item_name", "category"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_events",
            "description": "查询事件记录，包括已发生的和即将到来的提醒",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_type": {"type": "string", "description": "筛选事件类型，为空则查全部"},
                    "upcoming_only": {"type": "boolean", "description": "仅查询未来即将到来的事件"},
                    "limit": {"type": "integer", "description": "返回条数，默认10"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_expenses",
            "description": "查询花费记录并进行分析（自动对比上月、统计品牌等）",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "筛选类别，为空则汇总全部"},
                    "period": {"type": "string", "enum": ["本月", "上月", "近3个月", "近半年", "今年", "全部"],
                               "description": "时间范围，默认本月。用户问总开销/全部花费时用「全部」"},
                    "brand_analysis": {"type": "boolean", "description": "是否分析品牌排行，默认false"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_toys",
            "description": "查询猫咪的玩具喜好",
            "parameters": {
                "type": "object",
                "properties": {
                    "preference": {"type": "string", "enum": ["like", "dislike", "neutral"],
                                   "description": "筛选喜好，为空则全部"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_weight_trend",
            "description": "查询体重变化趋势",
            "parameters": {
                "type": "object",
                "properties": {
                    "months": {"type": "integer", "description": "最近几个月，默认6"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_inventory",
            "description": "查询库存状态，自动检查哪些物品低于阈值",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "筛选类别，为空则全部"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_cat_info",
            "description": "获取猫咪基本信息",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_cat_info",
            "description": "更新猫咪基本信息",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "猫咪名字"},
                    "birthday": {"type": "string", "description": "生日 YYYY-MM-DD"},
                    "bio": {"type": "string", "description": "猫咪简介/介绍"},
                    "status": {"type": "string", "description": "猫咪当前状态标签，如'减肥中''生病中''恢复期''挑食中''活泼好动''安静嗜睡'，传空字符串清除状态"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_context_setting",
            "description": "更新上下文设置（如驱虫间隔缩短天数等）",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "设置键名"},
                    "value": {"type": "string", "description": "设置值"}
                },
                "required": ["key", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_reminders",
            "description": "检查未来N天内需要提醒的事件",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "未来几天，默认7"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "simulate_travel",
            "description": "模拟出门/出差情景：检查库存是否足够、期间是否有疫苗/驱虫到期",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "出门日期 YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "归来日期 YYYY-MM-DD"}
                },
                "required": ["start_date", "end_date"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_food_days",
            "description": "计算猫粮还够吃几天（按每天约50g/kg猫咪体重估算）",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "adjust_deworm_schedule",
            "description": "当用户说家里来了新猫或带猫外出遛弯时，将下一次驱虫提醒缩短N天",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "enum": ["new_cat", "outdoor"],
                               "description": "缩短原因：new_cat=家里来了新猫，outdoor=带猫外出遛弯"},
                    "shorten_days": {"type": "integer", "description": "缩短天数，默认5天"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_expense",
            "description": "根据用户描述搜索匹配的花费记录，用于删除前的确认。支持按日期、类别、品牌、金额关键词搜索。返回匹配记录列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "花费类别关键词，如'医疗'、'猫粮'、'玩具'等"},
                    "date_hint": {"type": "string", "description": "日期线索，如'5月'、'最近'、'昨天'等"},
                    "brand_hint": {"type": "string", "description": "品牌或名称关键词"},
                    "amount_hint": {"type": "string", "description": "金额线索"},
                    "limit": {"type": "integer", "description": "返回条数，默认5"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_expense",
            "description": "删除指定的花费记录。必须先调用 search_expense 找到记录并让用户确认后，再用此函数执行删除。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expense_id": {"type": "integer", "description": "要删除的花费记录ID"},
                    "confirmed": {"type": "boolean", "description": "用户是否已确认删除，必须为true才能执行"}
                },
                "required": ["expense_id", "confirmed"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_expense",
            "description": "修改已有的花费记录。用户说'把XX改成YY''加上备注''补充一下'等修改操作时调用。需要先通过 query_expenses 找到要修改的记录ID，再调用此函数。只传需要修改的字段，其他字段保持不变。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expense_id": {"type": "integer", "description": "要修改的花费记录ID（从 query_expenses 或 search_expense 获取）"},
                    "date": {"type": "string", "description": "修改日期 YYYY-MM-DD（可选，不修改则不传）"},
                    "category": {"type": "string", "enum": ["猫粮", "玩具", "医疗", "猫砂", "零食", "用品", "其他"], "description": "修改分类（可选）"},
                    "brand": {"type": "string", "description": "修改品牌（可选）"},
                    "amount": {"type": "number", "description": "修改金额（可选）"},
                    "notes": {"type": "string", "description": "修改备注（可选）"}
                },
                "required": ["expense_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_photos",
            "description": "根据日期搜索猫咪照片。用户说'展示5月25号的照片''把今天的照片找出来'等时调用。返回照片URL列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date_query": {"type": "string", "description": "日期，如'2026-05-25'、'5月25号'、'今天'、'昨天'等。留空则返回最近上传的照片。"},
                    "limit": {"type": "integer", "description": "返回条数，默认20"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_trend_chart",
            "description": "获取花费趋势图表数据。用户说'看看趋势''月度花费对比''哪个月花钱最多''猫粮每个月花多少'等时调用。返回各月各分类的金额，前端自动渲染为折线图。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "筛选单个分类如'猫粮'，不传则返回全部。用户指定了特定分类时才传。"}
                }
            }
        }
    }
]


def build_system_prompt():
    """构建系统提示词"""
    today = date.today().strftime('%Y-%m-%d')
    today_plus1 = (date.today() + timedelta(days=1)).strftime('%Y-%m-%d')
    today_plus3 = (date.today() + timedelta(days=3)).strftime('%Y-%m-%d')
    cat_name = "猫咪"
    cat_birthday = None
    with get_db() as conn:
        row = conn.execute("SELECT * FROM cat_info WHERE id = 1").fetchone()
        if row:
            cat_name = row["name"] or "猫咪"
            cat_birthday = row["birthday"]

    cat_status = ""
    cat_name = "猫咪"
    cat_birthday = None
    with get_db() as conn:
        row = conn.execute("SELECT * FROM cat_info WHERE id = 1").fetchone()
        if row:
            cat_name = row["name"] or "猫咪"
            cat_birthday = row["birthday"]
            cat_status = (row["status"] or "").strip()

    cat_info_str = f"猫咪名字：{cat_name}，生日：{cat_birthday or '未设置'}，状态：{cat_status or '正常'}，今天日期：{today}，明天：{today_plus1}，三天后：{today_plus3}"

    return f"""你是一个贴心的猫咪私人助理。{cat_info_str}

你的职责：
1. 理解用户自然语言，调用 function 操作数据库
2. 回答问题时自动做分析对比（如对比上月花费、体重变化趋势等）
3. 给出温馨、有用的建议

规则：
- 事件记录（极重要）：
  * 用户说"打疫苗""驱虫""提醒""就医""拔牙""体检"等事件时，调用 add_event
  * 用户说"三天后提醒驱虫""明天打疫苗"等带时间提示的，必须把 next_due_date 算为具体日期（今天{today}、明天{today_plus1}、三天后{today_plus3}，以此类推）
  * 用户说"今天打了疫苗""昨天驱虫了"等（没给提醒时间），next_due_date 传 null，系统会自动算（疫苗+365天，驱虫+90天）
- 驱动提醒动态调整：用户说"家里来了新猫"或"带猫外出"时，调用 adjust_deworm_schedule
- 用户说出差/出门时，调用 simulate_travel 检查准备情况
- 当用户记录体重时（如"体重5.2kg""今天称了4.4kg""5月26日体重4.4kg"），调用 add_weight_record；日期未说明则默认今天({today})
- 用户询问体重变化时，调用 query_weight_trend
- 用户询问库存/猫粮问题时，调用 query_inventory 或 calculate_food_days
- 花费记录（极重要）：用户说"记录""记一笔""花了""买了""消费"等涉及花费时，必须调用 add_expense；金额必填，日期未说明则默认今天({today})
- 如果用户说"不喜欢"或"讨厌"某个玩具，调用 set_toy_preference 设 preference=dislike
- 如果用户说"喜欢"某个玩具，调用 set_toy_preference 设 preference=like
- 查询花费时自动做品牌分析和月度对比
- 用户问"总开销""全部花费""一共花了多少""历史总花费"时，调用 query_expenses(period="全部")

猫咪状态规则（重要）：
- 用户说猫咪在"减肥""生病""恢复期""挑食""活泼""安静嗜睡"等状态变化时，调用 update_cat_info(status="状态名")
- 用户说"取消状态""状态正常""恢复正常"时，调用 update_cat_info(status="") 清空状态
- 可选状态：减肥中、生病中、恢复期、挑食中、活泼好动、安静嗜睡

删除/修改花费规则（重要）：
- 删除：用户想删除花费时，不要直接删！先调用 search_expense 搜索，展示结果并确认，再调用 delete_expense
- 修改（极重要）：用户说"把XX改成YY""加上备注""补充一下""修改一下"等想要修改已有记录时：
  * 第一步：调用 query_expenses 或 search_expense 找到要修改的记录和它的 id
  * 第二步：调用 update_expense(expense_id=id, 只传需要修改的字段) 执行修改
  * 严禁假装修改！必须真正调用 update_expense 函数，否则数据不会更新！
  * 如果搜到多条可能的记录，先展示给用户确认是哪一条

图表查询规则：
- 用户询问"趋势""图表""对比""哪个月花钱最多""每月花费"等时，调用 get_trend_chart
- 如果用户明确指定了单个分类如"猫粮每个月花多少""猫砂每月花多少"，传 category 参数，返回该分类专属图表
- 如果用户问"对比猫粮和猫砂""所有分类的趋势""各类花费对比"，不传 category，返回全部对比图
- get_trend_chart 返回的结果中已包含 [CHART_DATA] 标签，直接输出即可（不要修改）

照片查询规则：
- 用户询问"展示某天的照片""看看某月的照片""有什么照片"时，调用 search_photos
- 将返回结果中的照片 URL 以可点击链接的形式展示，让用户可以直接打开看
- 如果搜不到照片，告知用户该日期没有照片

- 用口语化、温暖的中文回复，不要用 markdown 格式（纯文本即可）

每次回复时，先判断用户意图，调用合适的 function 获取/写入数据，然后基于返回的数据给出友好回答。"""


# ===================== Function 实现 =====================

def fn_add_event(type: str, date: str, next_due_date: str = None, notes: str = None):
    if next_due_date is None:
        dt = datetime.strptime(date, '%Y-%m-%d')
        if type == 'vaccine':
            next_due_date = (dt + timedelta(days=365)).strftime('%Y-%m-%d')
        elif type == 'deworm':
            next_due_date = (dt + timedelta(days=90)).strftime('%Y-%m-%d')

    with get_db() as conn:
        conn.execute(
            "INSERT INTO events (type, date, next_due_date, notes) VALUES (?, ?, ?, ?)",
            (type, date, next_due_date, notes)
        )
    return {"status": "ok", "message": f"已记录{type}事件，日期{date}" + (f"，下次到期{next_due_date}" if next_due_date else "")}


def fn_add_expense(date: str = None, category: str = None, brand: str = None, amount: float = 0, notes: str = None):
    if date is None:
        date = date.today().strftime('%Y-%m-%d')
    with get_db() as conn:
        # 去重：5分钟内完全相同的不重复插入（防止 AI 多轮 function calling 重复提交）
        existing = conn.execute(
            """SELECT id FROM expenses
               WHERE date = ? AND category = ? AND COALESCE(brand,'') = COALESCE(?,'')
               AND ABS(amount - ?) < 0.01 AND COALESCE(notes,'') = COALESCE(?,'')
               LIMIT 1""",
            (date, category, brand or '', amount, notes or '')
        ).fetchone()
        if existing:
            return {"status": "skipped", "message": f"已跳过重复记录：{date} {category} {brand or ''} ¥{amount:.1f}"}

        conn.execute(
            "INSERT INTO expenses (date, category, brand, amount, notes) VALUES (?, ?, ?, ?, ?)",
            (date, category, brand, amount, notes)
        )
    return {"status": "ok", "message": f"已记录：{date} {category} {brand or ''} ¥{amount:.1f}"}


def fn_set_toy_preference(name: str, preference: str, notes: str = None):
    with get_db() as conn:
        existing = conn.execute("SELECT id FROM toys WHERE name = ?", (name,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE toys SET preference = ?, last_used_date = ?, notes = COALESCE(?, notes) WHERE name = ?",
                (preference, date.today().strftime('%Y-%m-%d'), notes, name)
            )
        else:
            conn.execute(
                "INSERT INTO toys (name, preference, last_used_date, notes) VALUES (?, ?, ?, ?)",
                (name, preference, date.today().strftime('%Y-%m-%d'), notes)
            )
    pref_map = {'like': '喜欢', 'dislike': '不喜欢', 'neutral': '一般'}
    return {"status": "ok", "message": f"已记录猫咪对「{name}」的态度：{pref_map.get(preference, preference)}"}


def fn_add_weight_record(date: str = None, weight: float = 0, notes: str = None):
    if date is None:
        date = date.today().strftime('%Y-%m-%d')
    with get_db() as conn:
        conn.execute(
            "INSERT INTO weight_records (date, weight, notes) VALUES (?, ?, ?)",
            (date, weight, notes)
        )
    return {"status": "ok", "message": f"已记录体重：{date} {weight:.2f}kg"}


def fn_update_inventory(item_name: str, category: str, set_quantity: float = None,
                        threshold: float = None, unit: str = None, notes: str = None):
    with get_db() as conn:
        existing = conn.execute("SELECT * FROM inventory WHERE item_name = ?", (item_name,)).fetchone()
        if existing:
            new_qty = set_quantity if set_quantity is not None else existing["quantity"]
            new_threshold = threshold if threshold is not None else existing["threshold"]
            new_unit = unit if unit is not None else existing["unit"]
            conn.execute(
                "UPDATE inventory SET quantity = ?, threshold = ?, unit = ?, category = ?, notes = COALESCE(?, notes) WHERE item_name = ?",
                (new_qty, new_threshold, new_unit, category, notes, item_name)
            )
        else:
            conn.execute(
                "INSERT INTO inventory (item_name, category, quantity, threshold, unit, notes) VALUES (?, ?, ?, ?, ?, ?)",
                (item_name, category, set_quantity or 0, threshold or 1, unit or 'kg', notes)
            )

    # 检查是否低于阈值
    with get_db() as conn:
        item = conn.execute("SELECT * FROM inventory WHERE item_name = ?", (item_name,)).fetchone()
    low_warning = ""
    if item and item["quantity"] <= item["threshold"]:
        low_warning = f" ⚠️ {item_name} 库存({item['quantity']}{item['unit']})已低于阈值({item['threshold']}{item['unit']})，建议尽快补货！"

    return {"status": "ok", "message": f"已更新{item_name}库存" + low_warning}


def fn_query_events(event_type: str = None, upcoming_only: bool = False, limit: int = 10):
    today = date.today().strftime('%Y-%m-%d')
    with get_db() as conn:
        if upcoming_only:
            if event_type:
                rows = conn.execute(
                    "SELECT * FROM events WHERE type = ? AND next_due_date >= ? ORDER BY next_due_date LIMIT ?",
                    (event_type, today, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM events WHERE next_due_date >= ? ORDER BY next_due_date LIMIT ?",
                    (today, limit)
                ).fetchall()
        else:
            if event_type:
                rows = conn.execute(
                    "SELECT * FROM events WHERE type = ? ORDER BY date DESC LIMIT ?",
                    (event_type, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM events ORDER BY date DESC LIMIT ?",
                    (limit,)
                ).fetchall()

    result = rows_to_list(rows)
    # 计算倒计时
    for r in result:
        if r.get("next_due_date") and r["next_due_date"] >= today:
            nd = datetime.strptime(r["next_due_date"], '%Y-%m-%d')
            r["days_left"] = (nd - date.today()).days
    return {"count": len(result), "events": result}


def fn_query_expenses(category: str = None, period: str = "本月", brand_analysis: bool = False):
    month_start, month_end_str = get_month_range(period)

    with get_db() as conn:
        sql = "SELECT * FROM expenses WHERE 1=1"
        params = []
        if month_start:
            sql += " AND date >= ?"; params.append(month_start)
        if month_end_str:
            sql += " AND date <= ?"; params.append(month_end_str)
        if category:
            sql += " AND category = ?"; params.append(category)
        sql += " ORDER BY date DESC"
        rows = conn.execute(sql, params).fetchall()

    total = get_expense_total(start_date=month_start, end_date=month_end_str, category=category)

    result = {"period": period, "total": total, "count": len(rows), "items": rows_to_list(rows)}

    # 对比上月
    if period == "本月":
        prev_start, prev_end = get_month_range("上月")
        prev_total = get_expense_total(start_date=prev_start, end_date=prev_end, category=category)

        if prev_total > 0:
            change = ((total - prev_total) / prev_total) * 100
            result["prev_month_total"] = prev_total
            result["change_pct"] = round(change, 1)
        else:
            result["prev_month_total"] = 0
            result["change_pct"] = None

    # 品牌分析
    if brand_analysis:
        with get_db() as conn:
            brand_rows = conn.execute(
                "SELECT brand, SUM(amount) as total, COUNT(*) as cnt FROM expenses WHERE date >= ? AND date <= ? AND brand IS NOT NULL AND brand != '' GROUP BY brand ORDER BY total DESC LIMIT 10",
                (month_start, month_end_str)
            ).fetchall()
        result["brands"] = rows_to_list(brand_rows)

    return result


def fn_query_toys(preference: str = None):
    with get_db() as conn:
        if preference:
            rows = conn.execute("SELECT * FROM toys WHERE preference = ? ORDER BY last_used_date DESC", (preference,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM toys ORDER BY last_used_date DESC").fetchall()
    return {"count": len(rows), "toys": rows_to_list(rows)}


def fn_query_weight_trend(months: int = 6):
    start_date = (date.today() - timedelta(days=months * 30)).strftime('%Y-%m-%d')
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM weight_records WHERE date >= ? ORDER BY date ASC",
            (start_date,)
        ).fetchall()

    result = rows_to_list(rows)
    if len(result) >= 2:
        first_w = result[0]["weight"]
        last_w = result[-1]["weight"]
        change = last_w - first_w
        if change > 0.3:
            trend = "体重呈上升趋势，猫咪可能偏胖了，建议控制饮食"
        elif change < -0.3:
            trend = "体重呈下降趋势，需要关注猫咪健康"
        else:
            trend = "体重基本稳定，猫咪状态良好"
        return {
            "count": len(result),
            "records": result,
            "first_weight": first_w,
            "last_weight": last_w,
            "change_kg": round(change, 2),
            "trend_analysis": trend
        }
    return {"count": len(result), "records": result, "trend_analysis": "数据不足，需要更多记录"}


def fn_query_inventory(category: str = None):
    with get_db() as conn:
        if category:
            rows = conn.execute("SELECT * FROM inventory WHERE category = ?", (category,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM inventory").fetchall()

    result = rows_to_list(rows)
    low_items = []
    for r in result:
        if r["quantity"] <= r["threshold"]:
            low_items.append(r)

    return {
        "count": len(result),
        "items": result,
        "low_stock_items": low_items,
        "low_stock_count": len(low_items)
    }


def fn_get_cat_info():
    with get_db() as conn:
        row = conn.execute("SELECT * FROM cat_info WHERE id = 1").fetchone()
    if row:
        d = dict(row)
        if d.get("birthday"):
            bd = datetime.strptime(d["birthday"], '%Y-%m-%d')
            today = date.today()
            # 计算下一个生日倒计时
            next_birthday = bd.replace(year=today.year)
            if next_birthday.date() < today:
                next_birthday = bd.replace(year=today.year + 1)
            d["days_to_birthday"] = (next_birthday.date() - today).days
            d["age_years"] = today.year - bd.year
        return d
    return {"name": "未设置", "birthday": None}


def fn_update_cat_info(name: str = None, birthday: str = None, bio: str = None, status: str = None):
    with get_db() as conn:
        existing = conn.execute("SELECT * FROM cat_info WHERE id = 1").fetchone()
        if existing:
            n = name if name else existing["name"]
            b = birthday if birthday else existing["birthday"]
            bio_val = bio if bio is not None else existing["bio"]
            s = status if status is not None else existing["status"]
            conn.execute("UPDATE cat_info SET name = ?, birthday = ?, bio = ?, status = ? WHERE id = 1", (n, b, bio_val, s))
        else:
            conn.execute(
                "INSERT INTO cat_info (id, name, birthday, bio, status, created_at) VALUES (1, ?, ?, ?, ?, ?)",
                (name or "小咪", birthday, bio or "", status or "", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            )
    return {"status": "ok", "message": f"猫咪信息已更新"}


def fn_update_context_setting(key: str, value: str):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO context_settings (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
    return {"status": "ok", "message": f"设置 {key} = {value} 已保存"}


def fn_check_reminders(days: int = 7):
    cutoff = (date.today() + timedelta(days=days)).strftime('%Y-%m-%d')
    today_str = date.today().strftime('%Y-%m-%d')
    reminders = []

    with get_db() as conn:
        events = conn.execute(
            "SELECT * FROM events WHERE next_due_date >= ? AND next_due_date <= ? ORDER BY next_due_date",
            (today_str, cutoff)
        ).fetchall()

        for e in events:
            nd = datetime.strptime(e["next_due_date"], '%Y-%m-%d')
            days_left = (nd.date() - date.today()).days
            reminders.append({
                "type": e["type"],
                "event_date": e["date"],
                "due_date": e["next_due_date"],
                "days_left": days_left,
                "notes": e["notes"],
                "message": f"距离下次{e['type']}还有 {days_left} 天（{e['next_due_date']}）"
            })

        # 检查生日
        cat = conn.execute("SELECT * FROM cat_info WHERE id = 1").fetchone()
        if cat and cat["birthday"]:
            bd = datetime.strptime(cat["birthday"], '%Y-%m-%d')
            today = date.today()
            this_year_bd = bd.replace(year=today.year)
            if this_year_bd.date() < today:
                this_year_bd = bd.replace(year=today.year + 1)
            bd_days_left = (this_year_bd.date() - today).days
            if bd_days_left <= 7:
                reminders.append({
                    "type": "birthday",
                    "cat_name": cat["name"],
                    "age": today.year - bd.year + (1 if bd_days_left < 0 else 0),
                    "days_left": bd_days_left,
                    "message": f"🎂 还有 {bd_days_left} 天就是 {cat['name']} 的 {today.year - bd.year + (1 if bd_days_left < 0 else 0)} 岁生日啦！"
                })

        # 库存不足提醒
        inventory = conn.execute("SELECT * FROM inventory WHERE quantity <= threshold").fetchall()
        for inv in inventory:
            reminders.append({
                "type": "low_stock",
                "item": inv["item_name"],
                "quantity": inv["quantity"],
                "threshold": inv["threshold"],
                "unit": inv["unit"],
                "message": f"⚠️ {inv['item_name']} 库存不足（剩余 {inv['quantity']}{inv['unit']}，阈值 {inv['threshold']}{inv['unit']}）"
            })

    return {"days_ahead": days, "count": len(reminders), "reminders": reminders}


def fn_simulate_travel(start_date: str, end_date: str):
    start = datetime.strptime(start_date, '%Y-%m-%d')
    end = datetime.strptime(end_date, '%Y-%m-%d')
    travel_days = (end - start).days + 1

    suggestions = []
    urgent_items = []

    with get_db() as conn:
        # 检查库存
        inventory = conn.execute("SELECT * FROM inventory").fetchall()
        for inv in inventory:
            # 估算每日消耗（猫粮约50g/天, 猫砂约100g/天）
            if inv["category"] == "猫粮":
                daily = 0.05  # kg
                needed = daily * travel_days
                if inv["quantity"] < needed:
                    urgent_items.append({
                        "item": inv["item_name"],
                        "current": inv["quantity"],
                        "unit": inv["unit"],
                        "needed": round(needed, 1),
                        "shortage": round(needed - inv["quantity"], 1)
                    })
                    suggestions.append(f"猫粮「{inv['item_name']}」只剩 {inv['quantity']}{inv['unit']}，"
                                       f"出门 {travel_days} 天需要约 {needed:.1f}{inv['unit']}，建议提前购买！")
            elif inv["category"] == "猫砂":
                daily = 0.1
                needed = daily * travel_days
                if inv["quantity"] < needed:
                    suggestions.append(f"猫砂只剩 {inv['quantity']}{inv['unit']}，建议补充")

        # 检查该时段是否有事件到期
        events = conn.execute(
            "SELECT * FROM events WHERE next_due_date >= ? AND next_due_date <= ?",
            (start_date, end_date)
        ).fetchall()
        for e in events:
            suggestions.append(f"⚠️ {e['next_due_date']} 有 {e['type']} 到期，出差期间可能错过，建议提前安排！")

    return {
        "travel_days": travel_days,
        "urgent_items": urgent_items,
        "suggestions": suggestions,
        "summary": "；".join(suggestions) if suggestions else "库存充足，期间无到期提醒，可以放心出门！"
    }


def fn_calculate_food_days():
    with get_db() as conn:
        cat = conn.execute("SELECT * FROM cat_info WHERE id = 1").fetchone()
        foods = conn.execute("SELECT * FROM inventory WHERE category = '猫粮'").fetchall()

    # 默认猫咪 4kg，每天吃 50g/kg = 200g
    weight = 4.0
    if cat and cat["birthday"]:
        age = date.today().year - datetime.strptime(cat["birthday"], '%Y-%m-%d').year
        # 幼猫吃更多，老猫吃更少
        if age < 1:
            weight = 2.0
            daily_per_kg = 60
        elif age > 10:
            daily_per_kg = 40
        else:
            daily_per_kg = 50
    else:
        daily_per_kg = 50

    daily_consumption = (weight * daily_per_kg) / 1000  # kg
    results = []
    for f in foods:
        days = f["quantity"] / daily_consumption
        results.append({
            "item": f["item_name"],
            "quantity": f["quantity"],
            "unit": f["unit"],
            "estimated_days": round(days, 1),
            "message": f"「{f['item_name']}」还够吃约 {days:.0f} 天（按每天 {daily_consumption*1000:.0f}g 估算）"
        })

    if not results:
        results = [{"item": "未知", "message": "未记录猫粮库存，请先添加"}]

    return {"daily_estimate_kg": round(daily_consumption, 3), "foods": results}


def fn_adjust_deworm_schedule(reason: str = "new_cat", shorten_days: int = 5):
    with get_db() as conn:
        # 获取最近的驱虫事件
        deworm = conn.execute(
            "SELECT * FROM events WHERE type = 'deworm' AND next_due_date IS NOT NULL ORDER BY next_due_date DESC LIMIT 1"
        ).fetchone()

        if deworm:
            old_due = deworm["next_due_date"]
            new_due = datetime.strptime(old_due, '%Y-%m-%d') - timedelta(days=shorten_days)
            new_due_str = new_due.strftime('%Y-%m-%d')
            conn.execute(
                "UPDATE events SET next_due_date = ?, notes = COALESCE(notes || '; ', '') || ? WHERE id = ?",
                (new_due_str, f"因{reason}提前{shorten_days}天", deworm["id"])
            )
            reason_map = {"new_cat": "家里来了新猫", "outdoor": "带猫外出遛弯"}
            return {
                "status": "ok",
                "old_due_date": old_due,
                "new_due_date": new_due_str,
                "shorten_days": shorten_days,
                "message": f"因为{reason_map.get(reason, reason)}，下次驱虫已从 {old_due} 提前到 {new_due_str}（缩短 {shorten_days} 天）"
            }
        else:
            return {"status": "not_found", "message": "未找到驱虫记录，请先添加驱虫事件"}


def fn_search_expense(category: str = None, date_hint: str = None, brand_hint: str = None,
                      amount_hint: str = None, limit: int = 5):
    """搜索匹配的花费记录"""
    conditions = []
    params = []

    if category:
        conditions.append("category LIKE ?")
        params.append(f"%{category}%")
    if date_hint:
        if '月' in date_hint:
            m = re.search(r'(\d+)月', date_hint)
            if m:
                month = m.group(1).zfill(2)
                conditions.append("strftime('%m', date) = ?")
                params.append(month)
        elif '最近' in date_hint or '最新' in date_hint or '刚刚' in date_hint:
            pass  # 不加日期过滤，按 id desc 返回最新
        elif '昨天' in date_hint:
            yesterday = (date.today() - timedelta(days=1)).strftime('%Y-%m-%d')
            conditions.append("date = ?")
            params.append(yesterday)
    if brand_hint:
        conditions.append("brand LIKE ?")
        params.append(f"%{brand_hint}%")
    if amount_hint:
        try:
            amt = float(amount_hint)
            conditions.append("ABS(amount - ?) < 0.1")
            params.append(amt)
        except ValueError:
            pass

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"SELECT * FROM expenses {where} ORDER BY date DESC, id DESC LIMIT ?"
    params.append(limit)

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()

    results = rows_to_list(rows)
    return {"count": len(results), "records": results}


def fn_delete_expense(expense_id: int, confirmed: bool = False):
    """删除花费记录（需确认）"""
    if not confirmed:
        return {"status": "cancelled", "message": "请先确认要删除的记录"}

    with get_db() as conn:
        record = conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
        if not record:
            return {"status": "not_found", "message": f"未找到 id={expense_id} 的花费记录"}

        conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        info = f"日期：{record['date']}，分类：{record['category']}，金额：¥{record['amount']:.1f}，备注：{record['notes'] or '无'}"
        return {"status": "ok", "message": f"已成功删除该笔花费记录。（{info}）"}


def fn_update_expense(expense_id: int, date: str = None, category: str = None,
                      brand: str = None, amount: float = None, notes: str = None):
    """修改已有的花费记录。只更新用户明确提到的字段，未提到的保持不变。"""
    with get_db() as conn:
        record = conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
        if not record:
            return {"status": "not_found", "message": f"未找到 id={expense_id} 的花费记录"}

        updates = []
        params = []
        if date is not None:
            updates.append("date = ?"); params.append(date)
        if category is not None:
            updates.append("category = ?"); params.append(category)
        if brand is not None:
            updates.append("brand = ?"); params.append(brand)
        if amount is not None:
            updates.append("amount = ?"); params.append(amount)
        if notes is not None:
            updates.append("notes = ?"); params.append(notes)

        if not updates:
            return {"status": "no_change", "message": "没有指定要修改的字段"}

        params.append(expense_id)
        conn.execute(f"UPDATE expenses SET {', '.join(updates)} WHERE id = ?", params)

        updated = conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
        info = f"日期：{updated['date']}，分类：{updated['category']}，品牌：{updated['brand'] or '无'}，金额：¥{updated['amount']:.1f}，备注：{updated['notes'] or '无'}"
        return {"status": "ok", "message": f"已更新花费记录。（{info}）"}


def fn_search_photos(date_query: str = None, limit: int = 20):
    """搜索照片"""
    conditions = []
    params = []

    if date_query:
        # 解析日期
        if date_query in ('今天', '今日'):
            target = date.today().strftime('%Y-%m-%d')
        elif date_query in ('昨天', '昨日'):
            target = (date.today() - timedelta(days=1)).strftime('%Y-%m-%d')
        elif date_query in ('前天',):
            target = (date.today() - timedelta(days=2)).strftime('%Y-%m-%d')
        elif re.match(r'^\d{4}-\d{2}-\d{2}$', date_query):
            target = date_query
        else:
            # 尝试从中文提取日期: "5月25号" → 提取月和日
            m = re.search(r'(\d{1,2})\s*月\s*(\d{1,2})\s*[号日]?', date_query)
            if m:
                month = m.group(1).zfill(2)
                day = m.group(2).zfill(2)
                target = f"{date.today().year}-{month}-{day}"
            else:
                # 模糊匹配：用 LIKE 搜
                conditions.append("upload_date LIKE ?")
                params.append(f"%{date_query}%")
                target = None

        if target:
            conditions.append("upload_date = ?")
            params.append(target)

    # 排除头像文件
    conditions.append("filename != 'avatar.jpg'")
    where = "WHERE " + " AND ".join(conditions)
    sql = f"SELECT * FROM photos {where} ORDER BY upload_date DESC, id DESC LIMIT ?"
    params.append(limit)

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()

    results = []
    for r in rows:
        results.append({
            "id": r["id"],
            "filename": r["filename"],
            "original_name": r["original_name"],
            "upload_date": r["upload_date"],
            "url": f"/photos/{r['filename']}",
            "size_kb": r["file_size_kb"]
        })

    return {"count": len(results), "photos": results}


def fn_get_trend_chart(category: str = None):
    """获取月度趋势数据，返回 [CHART_DATA] 标记供前端渲染"""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT strftime('%Y-%m', date) as month,
                      category as cat,
                      SUM(amount) as total
               FROM expenses
               GROUP BY month, cat ORDER BY month"""
        ).fetchall()

    months_set = {}
    for r in rows:
        m = r["month"]
        if m not in months_set:
            months_set[m] = {}
        months_set[m][r["cat"]] = r["total"]

    months = sorted(months_set.keys())
    if not months:
        return {"message": "暂无花费数据，无法生成图表。"}

    all_cats = ["猫粮", "猫砂", "零食", "玩具", "医疗", "用品", "其他"]
    cats_to_show = [category] if category else [c for c in all_cats if any(months_set[m].get(c, 0) > 0 for m in months)]

    if category and not cats_to_show:
        return {"message": f"暂无 {category} 类别的花费数据。"}

    # 生成摘要（先求和再四舍五入，避免中间累积误差）
    totals = {c: round(sum(months_set[m].get(c, 0) for m in months), 1) for c in cats_to_show}
    # 找出花费最多的月份
    monthly_totals = {m: sum(months_set[m].values()) for m in months}
    max_month = max(monthly_totals, key=monthly_totals.get)

    summary_lines = []
    for c in cats_to_show:
        summary_lines.append(f"{c}总花费 ¥{totals[c]:.1f}")
    summary = "，".join(summary_lines)
    summary += f"。花钱最多的月份是 {max_month}（¥{monthly_totals[max_month]:.1f}）"

    chart_label = category if category else "全部"
    chart_msg = f"[CHART_DATA:{chart_label}]月度花费趋势[/CHART_DATA] {summary}。点击下方按钮查看趋势图。"

    return {"message": chart_msg}


# ===================== Function 调度 =====================

def execute_function(name: str, args: dict) -> dict:
    fn_map = {
        "add_event": fn_add_event,
        "add_expense": fn_add_expense,
        "set_toy_preference": fn_set_toy_preference,
        "add_weight_record": fn_add_weight_record,
        "update_inventory": fn_update_inventory,
        "query_events": fn_query_events,
        "query_expenses": fn_query_expenses,
        "query_toys": fn_query_toys,
        "query_weight_trend": fn_query_weight_trend,
        "query_inventory": fn_query_inventory,
        "get_cat_info": fn_get_cat_info,
        "update_cat_info": fn_update_cat_info,
        "update_context_setting": fn_update_context_setting,
        "check_reminders": fn_check_reminders,
        "simulate_travel": fn_simulate_travel,
        "calculate_food_days": fn_calculate_food_days,
        "adjust_deworm_schedule": fn_adjust_deworm_schedule,
        "search_expense": fn_search_expense,
        "delete_expense": fn_delete_expense,
        "update_expense": fn_update_expense,
        "search_photos": fn_search_photos,
        "get_trend_chart": fn_get_trend_chart,
    }

    func = fn_map.get(name)
    if func is None:
        return {"error": f"未知函数: {name}"}
    try:
        return func(**args)
    except Exception as e:
        return {"error": str(e)}


# ===================== DeepSeek API =====================

async def call_deepseek(messages: list) -> dict:
    """调用 DeepSeek API，支持 Function Calling"""
    if DEEPSEEK_API_KEY == "your-api-key-here":
        return {"role": "assistant", "content": "⚠️ 未配置 DeepSeek API Key。请在项目目录下创建 api_key.txt 文件写入你的 Key，或者设置环境变量 DEEPSEEK_API_KEY。"}

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "tools": FUNCTIONS,
        "tool_choice": "auto",
        "temperature": 0.7,
        "max_tokens": 2000
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(f"{DEEPSEEK_BASE_URL}/chat/completions", json=payload, headers=headers)
        if resp.status_code != 200:
            error_detail = resp.text[:500]
            return {"role": "assistant",
                    "content": f"⚠️ API 调用失败 (HTTP {resp.status_code}): {error_detail}"}
        return resp.json()


# ===================== FastAPI 路由 =====================

@app.get("/", response_class=HTMLResponse)
async def index():
    """返回前端页面"""
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return HTMLResponse("<h1>前端文件未找到，请创建 static/index.html</h1>")


@app.post("/chat")
async def chat(request: Request):
    """核心对话接口"""
    body = await request.json()
    user_message = body.get("message", "")
    history = body.get("history", [])

    if not user_message.strip():
        return JSONResponse({"reply": "请说点什么吧~", "history": history})

    system_prompt = build_system_prompt()
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    # 最多 3 轮 function calling，跟踪所有执行过的函数
    max_rounds = 3
    actions = []
    WRITE_FUNCTIONS = {'add_expense', 'add_event', 'add_weight_record', 'set_toy_preference',
                       'update_inventory', 'delete_expense', 'update_expense', 'update_cat_info'}
    for _ in range(max_rounds):
        response = await call_deepseek(messages)
        choice = response.get("choices", [{}])[0] if "choices" in response else response

        if "choices" not in response:
            # 错误
            assistant_msg = {"role": "assistant", "content": response.get("content", str(response))}
            history.append({"role": "user", "content": user_message})
            history.append(assistant_msg)
            return JSONResponse({"reply": assistant_msg["content"], "history": history,
                                 "actions": actions,
                                 "data_changed": bool(set(actions) & WRITE_FUNCTIONS)})

        msg = choice.get("message", {})
        tool_calls = msg.get("tool_calls", [])

        if tool_calls:
            messages.append({"role": "assistant", "tool_calls": tool_calls, "content": msg.get("content") or ""})

            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    fn_args = {}
                fn_result = execute_function(fn_name, fn_args)
                actions.append(fn_name)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(fn_result, ensure_ascii=False)
                })
        else:
            # 纯文本回复
            assistant_msg = {"role": "assistant", "content": msg.get("content", "")}
            history.append({"role": "user", "content": user_message})
            history.append(assistant_msg)
            return JSONResponse({"reply": msg.get("content", ""), "history": history,
                                 "actions": actions,
                                 "data_changed": bool(set(actions) & WRITE_FUNCTIONS)})

    # 达到最大轮次，最后再请求一次总结
    final_response = await call_deepseek(messages)
    final_choice = final_response.get("choices", [{}])[0] if "choices" in final_response else {}
    final_content = final_choice.get("message", {}).get("content", "处理中...")
    assistant_msg = {"role": "assistant", "content": final_content}
    history.append({"role": "user", "content": user_message})
    history.append(assistant_msg)
    return JSONResponse({"reply": final_content, "history": history,
                         "actions": actions,
                         "data_changed": bool(set(actions) & WRITE_FUNCTIONS)})


@app.post("/upload-photo")
async def upload_photo(files: List[UploadFile] = File(...)):
    """上传猫咪照片（支持多张）"""
    results = []
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    for i, file in enumerate(files):
        if not file.filename:
            continue

        ext = Path(file.filename).suffix or ".jpg"
        suffix = f"_{i+1}" if len(files) > 1 else ""
        save_name = f"cat_{timestamp}{suffix}{ext}"
        save_path = PHOTOS_DIR / save_name
        upload_date = date.today().strftime('%Y-%m-%d')

        content = await file.read()
        save_path.write_bytes(content)
        file_size = round(len(content) / 1024, 1)

        with get_db() as conn:
            conn.execute(
                "INSERT INTO photos (filename, original_name, upload_date, file_size_kb) VALUES (?, ?, ?, ?)",
                (save_name, file.filename, upload_date, file_size)
            )

        results.append({
            "status": "ok",
            "original_name": file.filename,
            "photo_path": f"/photos/{save_name}",
            "file_size_kb": file_size
        })

    if not results:
        return JSONResponse({"error": "未选择文件"}, status_code=400)

    return JSONResponse({
        "status": "ok",
        "count": len(results),
        "photos": results,
        "upload_date": date.today().strftime('%Y-%m-%d'),
        "message": f"已上传 {len(results)} 张照片（{date.today().strftime('%Y-%m-%d')}）"
    })


@app.post("/api/upload-avatar")
async def upload_avatar(file: UploadFile = File(...)):
    """上传猫咪头像（覆盖旧头像）"""
    if not file.filename:
        return JSONResponse({"error": "未选择文件"}, status_code=400)
    content = await file.read()
    AVATAR_PATH.write_bytes(content)
    file_size = round(len(content) / 1024, 1)
    today_str = date.today().strftime('%Y-%m-%d')

    # 同步保存到相册数据库
    with get_db() as conn:
        existing = conn.execute("SELECT id FROM photos WHERE filename = 'avatar.jpg'").fetchone()
        if existing:
            conn.execute("UPDATE photos SET upload_date = ?, file_size_kb = ?, original_name = ? WHERE filename = 'avatar.jpg'",
                         (today_str, file_size, file.filename))
        else:
            conn.execute(
                "INSERT INTO photos (filename, original_name, upload_date, file_size_kb) VALUES (?, ?, ?, ?)",
                ("avatar.jpg", file.filename, today_str, file_size)
            )

    return JSONResponse({
        "status": "ok",
        "avatar_url": f"/photos/avatar.jpg?t={int(datetime.now().timestamp())}"
    })


@app.get("/api/photos")
async def list_photos():
    """列出所有上传的照片（按时间倒序），从数据库读取以获取上传日期"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT filename, original_name, upload_date, file_size_kb FROM photos ORDER BY upload_date DESC, id DESC"
        ).fetchall()

    photos = []
    for r in rows:
        file_path = PHOTOS_DIR / r["filename"]
        url = f"/photos/{r['filename']}"
        size_kb = r["file_size_kb"] if r["file_size_kb"] else (round(file_path.stat().st_size / 1024, 1) if file_path.exists() else 0)
        photos.append({
            "filename": r["filename"],
            "original_name": r["original_name"],
            "upload_date": r["upload_date"],
            "url": url,
            "size_kb": size_kb
        })

    # 补充：数据库中没有但文件夹里有的照片
    db_names = {p["filename"] for p in photos}
    if PHOTOS_DIR.exists():
        for f in sorted(PHOTOS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.suffix.lower() in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'):
                if f.name not in db_names:
                    photos.append({
                        "filename": f.name,
                        "original_name": f.name,
                        "upload_date": datetime.fromtimestamp(f.stat().st_mtime).strftime('%Y-%m-%d'),
                        "url": f"/photos/{f.name}",
                        "size_kb": round(f.stat().st_size / 1024, 1)
                    })

    return JSONResponse({"count": len(photos), "photos": photos})


@app.get("/api/reminders")
async def get_reminders():
    """手动获取提醒"""
    return JSONResponse(fn_check_reminders(7))


@app.get("/api/debug_expenses")
async def debug_expenses():
    """调试接口：查看所有花费记录"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, date, category, brand, amount, notes FROM expenses ORDER BY date, id"
        ).fetchall()

    records = []
    total = 0
    for r in rows:
        total += r["amount"]
        records.append({
            "id": r["id"],
            "date": r["date"],
            "category": r["category"],
            "brand": r["brand"] or "",
            "amount": r["amount"],
            "notes": r["notes"] or ""
        })

    cat_totals = {}
    for r in rows:
        c = r["category"]
        cat_totals[c] = cat_totals.get(c, 0) + r["amount"]

    return JSONResponse({
        "total_records": len(rows),
        "total_amount": round(total, 2),
        "category_totals": {k: round(v, 2) for k, v in cat_totals.items()},
        "records": records
    })


@app.get("/api/debug_weights")
async def debug_weights():
    """调试接口：查看所有体重记录"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM weight_records ORDER BY date DESC"
        ).fetchall()
    return JSONResponse({
        "total_records": len(rows),
        "records": rows_to_list(rows)
    })


@app.delete("/api/expense/last")
async def delete_last_expense():
    """删除最新的一条花费记录"""
    with get_db() as conn:
        record = conn.execute("SELECT * FROM expenses ORDER BY id DESC LIMIT 1").fetchone()
        if not record:
            return JSONResponse({"status": "not_found", "message": "没有可删除的花费记录"}, status_code=404)
        conn.execute("DELETE FROM expenses WHERE id = ?", (record["id"],))
        info = f"日期：{record['date']}，分类：{record['category']}，金额：¥{record['amount']:.1f}"
        return JSONResponse({"status": "ok", "message": f"已删除最新记录：{info}"})


@app.delete("/api/expense/{expense_id}")
async def delete_expense_api(expense_id: int):
    """删除指定ID的花费记录"""
    with get_db() as conn:
        record = conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
        if not record:
            return JSONResponse({"status": "not_found", "message": f"未找到 id={expense_id} 的花费记录"}, status_code=404)
        conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        info = f"日期：{record['date']}，分类：{record['category']}，金额：¥{record['amount']:.1f}"
        return JSONResponse({"status": "ok", "message": f"已删除：{info}"})


@app.get("/api/expenses/recent")
async def get_recent_expenses():
    """返回最近5条花费记录"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, date, category, brand, amount, notes FROM expenses ORDER BY id DESC LIMIT 5"
        ).fetchall()
    records = []
    for r in rows:
        records.append({
            "id": r["id"],
            "date": r["date"],
            "category": r["category"],
            "brand": r["brand"] or "",
            "amount": r["amount"],
            "notes": r["notes"] or ""
        })
    return JSONResponse({"count": len(records), "records": records})


# ===================== 子分类规则 =====================

MEDICAL_SUB_RULES = [
    ("疫苗", ["疫苗", "猫三联", "妙三多"]),
    ("驱虫", ["驱虫", "大宠爱", "海乐妙", "爱沃克", "博莱恩", "福来恩", "拜耳"]),
    ("医保", ["医保", "保险"]),
]

SUPPLY_SUB_RULES = [
    ("猫砂盆", ["猫砂盆", "猫厕所"]),
    ("猫抓板", ["猫抓板", "抓板", "猫抓柱"]),
    ("猫碗", ["猫碗", "食碗", "饭碗", "水碗", "喂食器"]),
    ("猫包", ["猫包", "航空箱", "猫笼", "外出包"]),
    ("牵引绳", ["牵引绳", "牵引带", "胸背带", "遛猫绳"]),
    ("猫梳子", ["猫梳子", "梳子", "毛梳", "梳毛", "针梳", "排梳"]),
    ("伊丽莎白圈", ["伊丽莎白圈", "伊丽莎白", "防舔圈", "脖圈"]),
    ("猫砂铲", ["猫砂铲", "猫砂勺", "铲子"]),
    ("宠物垃圾袋", ["垃圾袋", "拾便袋", "便便袋"]),
    ("免洗手套湿巾", ["免洗", "手套", "湿巾", "清洁手套"]),
    ("费洛蒙喷雾", ["费洛蒙", "喷雾", "信息素", "安抚素"]),
]


def classify_subcategory(category, notes, brand):
    """根据分类将记录归入子分类"""
    text = ((notes or "") + " " + (brand or "")).strip()

    if category == "医疗":
        for name, keywords in MEDICAL_SUB_RULES:
            if any(kw in text for kw in keywords):
                return name
        return "其他医疗"

    if category == "用品":
        for name, keywords in SUPPLY_SUB_RULES:
            if any(kw in text for kw in keywords):
                return name
        return "其他用品"

    # 零食、玩具、猫粮、猫砂等 → 按品牌/名称分组
    return (brand or "").strip() or "未标注"


@app.get("/api/category_detail")
async def category_detail(category: str = ""):
    """分类下钻详情：支持子分类统计（医疗/用品）和品牌分组（其他）"""
    if not category:
        return JSONResponse({"error": "请指定分类"}, status_code=400)

    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, date, brand, amount, notes FROM expenses WHERE category = ? ORDER BY date DESC",
            (category,)
        ).fetchall()

    details = []
    groups = {}  # sub_name → {count, total, last_date, items}

    for r in rows:
        notes = r["notes"] or ""
        brand = r["brand"] or ""

        detail = {
            "id": r["id"],
            "date": r["date"],
            "brand": brand or "未标注",
            "amount": r["amount"],
            "notes": notes
        }
        details.append(detail)

        sub = classify_subcategory(category, notes, brand)
        if sub not in groups:
            groups[sub] = {"name": sub, "count": 0, "total": 0.0, "last_date": "", "items": []}
        g = groups[sub]
        g["count"] += 1
        g["total"] += r["amount"]
        if not g["last_date"] or r["date"] > g["last_date"]:
            g["last_date"] = r["date"]
        g["items"].append(detail)

    # Round totals; sort groups by total desc
    group_list = []
    for g in groups.values():
        g["total"] = round(g["total"], 1)
        group_list.append(g)
    group_list.sort(key=lambda x: x["total"], reverse=True)

    # 对于品牌分组的类别，提取重量信息
    if category in ("猫粮", "猫砂"):
        for g in group_list:
            for item in g["items"]:
                if item["notes"]:
                    m = re.search(r'[\d.]+[Kk]?[Gg]', str(item["notes"]))
                    if m:
                        g["weight_info"] = m.group()
                        break

    return JSONResponse({
        "category": category,
        "group_count": len(group_list),
        "groups": group_list,
        "detail_count": len(details),
        "details": details
    })


@app.get("/api/category_trend")
async def category_trend(category: str = ""):
    """返回单个分类的月度花费趋势"""
    if not category:
        return JSONResponse({"error": "请指定分类"}, status_code=400)

    with get_db() as conn:
        rows = conn.execute(
            """SELECT strftime('%Y-%m', date) as month,
                      SUM(amount) as total
               FROM expenses WHERE category = ?
               GROUP BY month ORDER BY month""",
            (category,)
        ).fetchall()

    months = [r["month"] for r in rows]
    totals = [round(r["total"], 1) for r in rows]

    return JSONResponse({
        "category": category,
        "months": months,
        "totals": totals
    })


@app.get("/api/monthly_trend")
async def monthly_trend():
    """月度花费趋势：返回各月各分类的金额"""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT strftime('%Y-%m', date) as month,
                      category,
                      SUM(amount) as total
               FROM expenses
               GROUP BY month, category
               ORDER BY month"""
        ).fetchall()

    months_set = {}
    for r in rows:
        m = r["month"]
        if m not in months_set:
            months_set[m] = {}
        months_set[m][r["category"]] = round(r["total"], 1)

    months = sorted(months_set.keys())
    categories = ["猫粮", "猫砂", "零食", "玩具", "医疗", "用品", "其他"]

    result = {"months": months, "categories": {}}
    for cat in categories:
        result["categories"][cat] = [months_set[m].get(cat, 0) for m in months]

    # 每月总计
    result["totals"] = [sum(months_set[m].values()) for m in months]

    return JSONResponse(result)


@app.get("/api/item_detail")
async def item_detail(category: str = "", item_name: str = ""):
    """查看单个项目（品牌/名称）的购买记录"""
    if not category or not item_name:
        return JSONResponse({"error": "请指定分类和项目名称"}, status_code=400)

    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, date, brand, amount, notes FROM expenses
               WHERE category = ? AND (brand = ? OR brand LIKE ?)
               ORDER BY date DESC""",
            (category, item_name, f"%{item_name}%")
        ).fetchall()

    records = []
    total = 0
    for r in rows:
        total += r["amount"]
        records.append({
            "id": r["id"],
            "date": r["date"],
            "brand": r["brand"] or "",
            "amount": r["amount"],
            "notes": r["notes"] or ""
        })

    return JSONResponse({
        "category": category,
        "item_name": item_name,
        "count": len(records),
        "total": round(total, 1),
        "records": records
    })


@app.get("/api/dashboard")
async def dashboard():
    """仪表盘数据"""
    with get_db() as conn:
        cat = conn.execute("SELECT * FROM cat_info WHERE id = 1").fetchone()

        # 即将到期事件数
        event_count = conn.execute(
            "SELECT COUNT(*) as c FROM events WHERE next_due_date >= ? AND next_due_date <= ?",
            (date.today().strftime('%Y-%m-%d'),
             (date.today() + timedelta(days=7)).strftime('%Y-%m-%d'))
        ).fetchone()["c"]

        inventory_low = conn.execute("SELECT COUNT(*) as c FROM inventory WHERE quantity <= threshold").fetchone()["c"]

        # 本月花费 — 使用统一日期范围函数
        month_start, month_end_str = get_month_range("本月")
        monthly_expense = get_expense_total(start_date=month_start, end_date=month_end_str)

        # 总开销
        total_expense = get_expense_total()

        last_weight = conn.execute(
            "SELECT * FROM weight_records ORDER BY date DESC LIMIT 1"
        ).fetchone()

    return JSONResponse({
        "cat_name": dict(cat) if cat else None,
        "cat_breed": "奶牛猫",
        "cat_gender": "女",
        "upcoming_events": event_count,
        "low_stock_count": inventory_low,
        "monthly_expense": round(monthly_expense, 1),
        "total_expense": round(total_expense, 1),
        "last_weight": dict_from_row(last_weight)
    }, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/api/weight_trend")
async def weight_trend():
    """体重变化趋势数据"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT date, weight FROM weight_records ORDER BY date ASC"
        ).fetchall()
    if not rows:
        return JSONResponse({"dates": [], "weights": []})
    return JSONResponse({
        "dates": [r["date"] for r in rows],
        "weights": [r["weight"] for r in rows]
    })


@app.post("/api/cat_bio")
async def update_cat_bio(request: Request):
    """更新猫咪简介"""
    body = await request.json()
    bio = body.get("bio", "")
    with get_db() as conn:
        existing = conn.execute("SELECT * FROM cat_info WHERE id = 1").fetchone()
        if existing:
            conn.execute("UPDATE cat_info SET bio = ? WHERE id = 1", (bio,))
        else:
            conn.execute(
                "INSERT INTO cat_info (id, name, birthday, bio, created_at) VALUES (1, ?, ?, ?, ?)",
                ("小咪", None, bio, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            )
    return JSONResponse({"status": "ok", "bio": bio})


@app.post("/api/cat_status")
async def update_cat_status(request: Request):
    """更新猫咪状态标签"""
    body = await request.json()
    status = body.get("status", "")
    with get_db() as conn:
        existing = conn.execute("SELECT * FROM cat_info WHERE id = 1").fetchone()
        if existing:
            conn.execute("UPDATE cat_info SET status = ? WHERE id = 1", (status,))
        else:
            conn.execute(
                "INSERT INTO cat_info (id, name, birthday, bio, status, created_at) VALUES (1, ?, ?, ?, ?, ?)",
                ("小咪", None, "", status, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            )
    return JSONResponse({"status": "ok", "cat_status": status})


@app.get("/api/expense_summary")
async def expense_summary():
    """花费分类统计"""
    month_start, month_end_str = get_month_range("本月")

    # 统一函数
    total_all = get_expense_total()
    total_month = get_expense_total(start_date=month_start, end_date=month_end_str)
    all_time_cats = get_expense_by_category()
    this_month_cats = get_expense_by_category(start_date=month_start, end_date=month_end_str)

    def build_cat_dict(rows):
        result = {"猫粮": 0, "玩具": 0, "医疗": 0, "猫砂": 0, "零食": 0, "用品": 0, "其他": 0}
        for r in rows:
            cat = r["category"] if r["category"] in result else "其他"
            result[cat] = round(r["total"], 1)
        return result

    return JSONResponse({
        "total_all_time": round(total_all, 1),
        "total_this_month": round(total_month, 1),
        "categories_all_time": build_cat_dict(all_time_cats),
        "categories_this_month": build_cat_dict(this_month_cats)
    })


@app.get("/api/upcoming_events")
async def upcoming_events():
    """未来7天到期事件"""
    today = date.today().strftime('%Y-%m-%d')
    cutoff = (date.today() + timedelta(days=7)).strftime('%Y-%m-%d')
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM events
               WHERE next_due_date >= ? AND next_due_date <= ?
               ORDER BY next_due_date""",
            (today, cutoff)
        ).fetchall()

    events = []
    for r in rows:
        nd = datetime.strptime(r["next_due_date"], '%Y-%m-%d')
        days_left = (nd.date() - date.today()).days
        type_map = {'vaccine': '疫苗', 'deworm': '驱虫', 'vet_visit': '就医', 'dental': '拔牙/口腔', 'birthday': '生日'}
        events.append({
            "id": r["id"],
            "type": type_map.get(r["type"], r["type"]),
            "date": r["date"],
            "due_date": r["next_due_date"],
            "days_left": days_left,
            "notes": r["notes"]
        })

    return JSONResponse({"count": len(events), "events": events})


@app.get("/api/calendar")
async def calendar_data(year: int, month: int):
    """日历视图数据：返回指定月份的事件、花费、体重、照片"""
    month_str = f"{year}-{month:02d}"
    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1
    month_end_str = f"{next_year}-{next_month:02d}-01"

    with get_db() as conn:
        # 事件（包括 event_date 在该月的，和 next_due_date 在该月的）
        events = conn.execute(
            """SELECT id, type, date, next_due_date, notes FROM events
               WHERE (date >= ? AND date < ?)
                  OR (next_due_date >= ? AND next_due_date < ?)
               ORDER BY date""",
            (month_str + "-01", month_end_str, month_str + "-01", month_end_str)
        ).fetchall()

        # 花费
        expenses = conn.execute(
            "SELECT id, date, category, brand, amount, notes FROM expenses WHERE date >= ? AND date < ? ORDER BY date",
            (month_str + "-01", month_end_str)
        ).fetchall()

        # 体重
        weights = conn.execute(
            "SELECT id, date, weight, notes FROM weight_records WHERE date >= ? AND date < ? ORDER BY date",
            (month_str + "-01", month_end_str)
        ).fetchall()

        # 照片日期
        photo_rows = conn.execute(
            "SELECT DISTINCT upload_date FROM photos WHERE upload_date >= ? AND upload_date < ? AND filename != 'avatar.jpg' ORDER BY upload_date",
            (month_str + "-01", month_end_str)
        ).fetchall()

    return JSONResponse({
        "year": year,
        "month": month,
        "events": rows_to_list(events),
        "expenses": rows_to_list(expenses),
        "weights": rows_to_list(weights),
        "photo_dates": [r["upload_date"] for r in photo_rows]
    })


@app.put("/api/event/{event_id}/move")
async def move_event(event_id: int, request: Request):
    """拖拽移动事件日期"""
    body = await request.json()
    new_date = body.get("date", "")
    if not new_date:
        return JSONResponse({"error": "请提供新日期"}, status_code=400)

    with get_db() as conn:
        event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if not event:
            return JSONResponse({"error": "事件不存在"}, status_code=404)

        # 更新事件日期及 next_due_date（保持间隔不变）
        old_date = event["date"]
        old_due = event["next_due_date"]

        if old_due:
            # 计算原间隔天数，新 next_due_date = 新日期 + 间隔
            try:
                old_date_dt = datetime.strptime(old_date, '%Y-%m-%d')
                old_due_dt = datetime.strptime(old_due, '%Y-%m-%d')
                gap = (old_due_dt - old_date_dt).days
                new_date_dt = datetime.strptime(new_date, '%Y-%m-%d')
                new_due = (new_date_dt + timedelta(days=gap)).strftime('%Y-%m-%d')
            except:
                new_due = old_due
        else:
            new_due = None

        conn.execute(
            "UPDATE events SET date = ?, next_due_date = ?, notes = COALESCE(notes || '; ', '') || ? WHERE id = ?",
            (new_date, new_due, f"拖拽调整日期（原{old_date}）", event_id)
        )

    type_map = {'vaccine': '疫苗', 'deworm': '驱虫', 'vet_visit': '就医', 'dental': '拔牙/口腔', 'birthday': '生日'}
    return JSONResponse({
        "status": "ok",
        "message": f"已调整{type_map.get(event['type'], event['type'])}日期：{old_date} → {new_date}"
    })


# ===================== 定时提醒 =====================

def daily_reminder_job():
    """每天 8:00 执行的定时任务"""
    reminders = fn_check_reminders(7)
    today_str = date.today().strftime('%Y-%m-%d')

    print(f"\n{'='*50}")
    print(f"📅 {today_str} 猫咪助理提醒")
    print(f"{'='*50}")

    if reminders["count"] == 0:
        print("✅ 未来 7 天暂无提醒，一切安好！")
    else:
        for r in reminders["reminders"]:
            msg = r.get("message", "")
            rtype = r.get("type", "")
            if rtype == "birthday":
                # 生日语气多样化
                cat_name = r.get("cat_name", "猫咪")
                age = r.get("age", 1)
                days_left = int(r.get("days_left", 0))
                tones = [
                    f"🎂 还有 {days_left} 天就是 {cat_name} 的 {age} 岁生日啦！需要我帮你物色猫咪蛋糕吗？",
                    f"🎉 距离 {cat_name} 的 {age} 岁生日只剩 {days_left} 天咯！准备好小鱼干和礼物了吗？",
                    f"🎈 {cat_name} 马上要过 {age} 岁生日啦（{days_left} 天倒计时）！是时候计划庆祝了~",
                    f"🎁 叮咚！{cat_name} 的生日提醒：还有 {days_left} 天！记得预定猫咪蛋糕哦~",
                    f"🐱 {cat_name} 的 {age} 岁生日倒计时 {days_left} 天！小主正在期待呢~",
                    f"🎊 {days_left} 天后就是 {cat_name} 的生日！要开始准备惊喜了哦~",
                    f"🍰 {cat_name} 的生日就在 {days_left} 天后了！猫罐头要管够！"
                ]
                msg = random.choice(tones)

            print(f"  {msg}")

    print(f"{'='*50}\n")


async def scheduler_loop():
    """asyncio 定时任务：每天 8:00 执行提醒"""
    # 启动时立即执行一次
    daily_reminder_job()

    while True:
        now = datetime.now()
        # 计算到下一个 8:00 的秒数
        next_run = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        wait_seconds = (next_run - now).total_seconds()
        await asyncio.sleep(wait_seconds)
        daily_reminder_job()


async def start_scheduler():
    """启动后台定时任务"""
    asyncio.create_task(scheduler_loop())
    print("✅ 定时任务已启动（每天 8:00 检查提醒）")


# ===================== 启动 =====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await start_scheduler()
    print("\n🐱 猫咪私人助理 启动中...")
    print(f"   📱 本机访问: http://localhost:8001")
    print(f"   📱 局域网访问: http://<你的电脑IP>:8001")
    print(f"   🔑 请确保已设置 DEEPSEEK_API_KEY 环境变量\n")
    yield

app.router.lifespan_context = lifespan


def kill_port_process(port: int):
    """释放指定端口：查找并终止占用进程（Windows）"""
    import subprocess
    try:
        result = subprocess.run(
            ['netstat', '-ano'], capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.splitlines():
            if f':{port}' in line and 'LISTENING' in line:
                parts = line.strip().split()
                pid = parts[-1]
                if pid.isdigit() and int(pid) != os.getpid():
                    print(f"  ⚠️ 端口 {port} 被进程 PID={pid} 占用，正在终止...")
                    subprocess.run(['taskkill', '/F', '/PID', pid],
                                   capture_output=True, timeout=10)
                    print(f"  ✅ 旧进程已终止，端口已释放")
                    return
    except Exception as e:
        print(f"  ⚠️ 端口检查失败: {e}")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8001))

    # Windows 本地开发：自动释放被占用的端口
    if sys.platform == 'win32':
        import time
        kill_port_process(port)
        time.sleep(0.5)

    print(f"🚀 启动服务，端口: {port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
