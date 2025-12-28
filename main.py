import asyncio
import logging
import os
import sys
import secrets
import time
import random
import json
import io
import csv
from functools import wraps
from datetime import datetime, timedelta
import aiosqlite
import pytz
from quart import Quart, render_template_string, request, redirect, url_for, session, abort, Response, jsonify
from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession
from telethon.tl.types import UserStatusOnline, UserStatusRecently, UserStatusOffline
from hypercorn.config import Config
from hypercorn.asyncio import serve

# --- CONFIGURATION ---
DEFAULT_API_ID = os.getenv("API_ID", "")
DEFAULT_API_HASH = os.getenv("API_HASH", "")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
TIMEZONE = 'Asia/Kolkata'
TZ = pytz.timezone(TIMEZONE)

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("Spectre.Heritage")

# --- APP SETUP ---
app = Quart(__name__)
app.secret_key = SECRET_KEY

# Global vars
temp_client = None
phone_number = None
phone_code_hash = None
runtime_api_id = int(DEFAULT_API_ID) if DEFAULT_API_ID.isdigit() else 0
runtime_api_hash = DEFAULT_API_HASH

# --- DATABASE SCHEMA ---
DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE,
    password TEXT,
    is_admin BOOLEAN DEFAULT 0,
    status TEXT DEFAULT 'active',
    max_targets INTEGER DEFAULT 3,
    expiry_date DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER DEFAULT 1,
    tg_id INTEGER,
    tg_username TEXT,
    phone TEXT,
    display_name TEXT,
    last_status TEXT,
    last_seen DATETIME,
    is_tracking BOOLEAN DEFAULT 1,
    pic_path TEXT,
    notes TEXT,
    notifications BOOLEAN DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER,
    status TEXT,
    start_time DATETIME,
    end_time DATETIME,
    duration TEXT,
    FOREIGN KEY(target_id) REFERENCES targets(id)
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER,
    event_type TEXT,
    timestamp DATETIME,
    metadata TEXT
);
"""

# --- UI TEMPLATE (OLD MONEY / HERITAGE THEME) ---
HTML_BASE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SPECTRE // HERITAGE</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;600;700&family=Lora:ital,wght@0,400;0,500;0,600;1,400&family=Cormorant+Garamond:wght@400;600&display=swap" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        :root {
            /* OLD MONEY PALETTE */
            --bg: #F4F1EA;           /* Warm Alabaster/Cream */
            --sidebar-bg: #1A2F23;   /* Deep Hunter Green */
            --surface: #FFFFFF;      /* Crisp White */
            --text-main: #1C1C1C;    /* Charcoal */
            --text-muted: #5A5A5A;   /* Slate Grey */
            --accent: #1A2F23;       /* Hunter Green */
            --gold: #C5A059;         /* Muted Antique Gold */
            --border: #D8D4C8;       /* Stone Grey */
            
            --success: #2D5A27;      /* Forest Green (Online) */
            --danger: #8B0000;       /* Deep Burgundy (Error/Delete) */
            
            --shadow-subtle: 0 4px 6px rgba(26, 47, 35, 0.05);
            --ease: cubic-bezier(0.25, 0.46, 0.45, 0.94);
        }
        
        * { box-sizing: border-box; outline: none; }
        
        body { 
            background: var(--bg); 
            color: var(--text-main); 
            font-family: 'Lora', serif; 
            margin: 0; 
            display: flex;
            height: 100vh;
            overflow: hidden;
            animation: fadeIn 1s var(--ease) forwards;
        }

        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
        @keyframes slideUp { from { transform: translateY(15px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }

        /* SIDEBAR (The Club) */
        .sidebar {
            width: 280px;
            background: var(--sidebar-bg);
            border-right: 1px solid var(--gold);
            display: flex;
            flex-direction: column;
            padding: 3rem 2rem;
            flex-shrink: 0;
            color: #E0D8C8;
            box-shadow: 4px 0 15px rgba(0,0,0,0.1);
            z-index: 10;
        }
        
        .brand {
            font-family: 'Cinzel', serif;
            font-size: 1.8rem;
            font-weight: 700;
            margin-bottom: 4rem;
            color: var(--gold);
            text-decoration: none;
            text-transform: uppercase;
            letter-spacing: 0.15em;
            text-align: center;
            border-bottom: 1px solid rgba(197, 160, 89, 0.3);
            padding-bottom: 2rem;
        }
        
        .menu { display: flex; flex-direction: column; gap: 0.8rem; flex: 1; }
        
        .menu-label { 
            font-family: 'Cinzel', serif;
            font-size: 0.65rem; 
            color: rgba(224, 216, 200, 0.5); 
            text-transform: uppercase; 
            letter-spacing: 0.2em; 
            margin: 1.5rem 0 0.5rem 0;
        }
        
        .nav-item {
            padding: 0.8rem 0;
            color: #E0D8C8;
            text-decoration: none;
            font-size: 0.85rem;
            font-weight: 500;
            display: flex; align-items: center; gap: 15px;
            transition: all 0.3s ease;
            border-bottom: 1px solid transparent;
            font-family: 'Lora', serif;
            letter-spacing: 0.05em;
        }
        .nav-item:hover { color: var(--gold); transform: translateX(5px); }
        .nav-item.active { color: var(--gold); border-bottom: 1px solid var(--gold); }
        .nav-item i { width: 20px; text-align: center; font-size: 0.9rem; }

        .user-panel {
            border-top: 1px solid rgba(197, 160, 89, 0.3);
            padding-top: 2rem;
            margin-top: auto;
            display: flex; align-items: center; gap: 12px;
        }
        .avatar { 
            width: 38px; height: 38px; 
            background: var(--gold); color: var(--sidebar-bg);
            border-radius: 50%; 
            display: flex; align-items: center; justify-content: center; 
            font-family: 'Cinzel', serif; font-weight: 700;
        }

        /* MAIN CONTENT */
        .main {
            flex: 1;
            overflow-y: auto;
            padding: 4rem 5rem;
            background-image: radial-gradient(var(--border) 1px, transparent 1px);
            background-size: 30px 30px;
        }

        h1 { 
            font-family: 'Cinzel', serif; 
            font-size: 2.5rem; 
            margin-bottom: 0.5rem; 
            color: var(--accent); 
            font-weight: 400;
            letter-spacing: 0.05em;
        }
        .subtitle { 
            color: var(--text-muted); 
            font-size: 0.9rem; 
            margin-bottom: 3rem; 
            font-style: italic; 
            font-family: 'Lora', serif;
            border-left: 2px solid var(--gold);
            padding-left: 1rem;
        }

        /* CARDS (Stationery Look) */
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 2rem; margin-bottom: 3rem; }
        .stat-card { 
            background: var(--surface); 
            border: 1px solid var(--border); 
            padding: 2rem; 
            box-shadow: var(--shadow-subtle);
            position: relative;
            transition: transform 0.4s var(--ease);
            animation: slideUp 0.8s var(--ease) backwards;
        }
        .stat-card::after {
            content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 3px; background: var(--accent);
        }
        .stat-card:hover { transform: translateY(-5px); border-color: var(--gold); }
        
        .stat-val { font-size: 2.2rem; font-weight: 400; margin: 1rem 0; font-family: 'Cormorant Garamond', serif; color: var(--accent); }
        .stat-label { font-size: 0.7rem; text-transform: uppercase; color: var(--text-muted); font-family: 'Cinzel', serif; letter-spacing: 0.1em; }

        /* TABLES (Ledger Look) */
        .table-container { 
            background: var(--surface); border: 1px solid var(--border); 
            box-shadow: var(--shadow-subtle); animation: slideUp 0.8s var(--ease) 0.3s backwards;
        }
        table { width: 100%; border-collapse: collapse; }
        th { 
            text-align: left; padding: 1.5rem; 
            background: #F9F7F2; 
            border-bottom: 2px solid var(--border); 
            font-family: 'Cinzel', serif; font-size: 0.7rem; font-weight: 700; color: var(--text-main); 
            letter-spacing: 0.1em; 
        }
        td { 
            padding: 1.5rem; border-bottom: 1px solid var(--border); 
            font-size: 0.95rem; color: var(--text-muted); 
            font-family: 'Cormorant Garamond', serif; font-weight: 600;
        }
        tr:hover td { background: #FCFAF5; color: var(--accent); }

        /* BUTTONS (Classy) */
        .btn {
            background: var(--accent); color: var(--gold); 
            padding: 0.8rem 1.8rem; border: 1px solid var(--accent);
            text-decoration: none; font-size: 0.75rem; font-weight: 700; 
            display: inline-flex; align-items: center; gap: 10px; cursor: pointer; 
            font-family: 'Cinzel', serif; letter-spacing: 0.1em; text-transform: uppercase;
            transition: all 0.3s ease;
        }
        .btn:hover { background: var(--gold); color: var(--accent); border-color: var(--gold); }
        
        .btn-outline { background: transparent; border: 1px solid var(--text-muted); color: var(--text-main); }
        .btn-outline:hover { border-color: var(--gold); color: var(--gold); }
        
        input, select, textarea {
            width: 100%; padding: 1rem; 
            border: 1px solid var(--border); background: #FCFAF5;
            font-family: 'Lora', serif; font-size: 1rem; color: var(--text-main);
            transition: all 0.3s ease;
        }
        input:focus { border-color: var(--gold); background: #fff; box-shadow: 0 0 0 1px var(--gold) inset; }
        label { 
            display: block; font-size: 0.7rem; font-family: 'Cinzel', serif; 
            margin-bottom: 0.8rem; color: var(--text-muted); letter-spacing: 0.1em; 
        }
        .form-group { margin-bottom: 2rem; }

        /* UTILS */
        .status-badge { 
            padding: 5px 12px; font-size: 0.65rem; font-family: 'Cinzel', serif;
            text-transform: uppercase; display: inline-block; letter-spacing: 0.1em; border: 1px solid;
        }
        .on { color: var(--success); border-color: var(--success); background: rgba(45, 90, 39, 0.05); }
        .off { color: #999; border-color: #ccc; background: transparent; }
        .mono { font-family: 'Cormorant Garamond', serif; font-size: 1rem; font-style: italic; color: #888; }
        
        .search-bar { position: relative; width: 250px; }
        .search-bar input { padding-left: 2rem; border-bottom: 1px solid var(--border); border-top: none; border-left: none; border-right: none; background: transparent; }
        .search-bar i { position: absolute; left: 0; top: 50%; transform: translateY(-50%); color: var(--gold); font-size: 0.9rem; }

        /* LOGIN */
        .login-wrap { display: flex; height: 100vh; align-items: center; justify-content: center; background: var(--bg); }
        .login-box { 
            width: 100%; max-width: 450px; padding: 4rem; 
            background: var(--surface); border: 1px solid var(--gold); 
            box-shadow: 0 20px 40px rgba(26, 47, 35, 0.1);
            position: relative;
        }
        .login-box::before {
            content: ''; position: absolute; top: 10px; left: 10px; right: 10px; bottom: 10px; 
            border: 1px solid var(--border); pointer-events: none;
        }
    </style>
</head>
<body>
    {% if not hide_sidebar %}
    <div class="sidebar">
        <a href="/dashboard" class="brand">SPECTRE</a>
        
        <div class="menu">
            <div class="menu-label">Ledger</div>
            <a href="/dashboard" class="nav-item {{ 'active' if active_page == 'dashboard' else '' }}"><i class="fas fa-columns"></i> DASHBOARD</a>
            <a href="/intelligence" class="nav-item {{ 'active' if active_page == 'intelligence' else '' }}"><i class="fas fa-chess-board"></i> INTELLIGENCE</a>
            <a href="/logs" class="nav-item {{ 'active' if active_page == 'logs' else '' }}"><i class="fas fa-scroll"></i> ARCHIVE</a>
            
            <div class="menu-label">Protocol</div>
            <a href="/settings" class="nav-item {{ 'active' if active_page == 'settings' else '' }}"><i class="fas fa-cogs"></i> CONFIGURATION</a>
            {% if session.get('is_admin') %}
            <a href="/users" class="nav-item {{ 'active' if active_page == 'users' else '' }}"><i class="fas fa-users"></i> MEMBERSHIP</a>
            {% endif %}
        </div>

        <div class="user-panel">
            <div class="avatar">{{ session.get('username', 'A')[0].upper() }}</div>
            <div style="flex:1;">
                <div style="font-family:'Cinzel', serif; font-size:0.8rem; color:var(--gold);">AGENT {{ session.get('username', 'UNK').upper() }}</div>
                <div style="font-size:0.65rem; color:#888; font-style:italic;">Clearance Level 5</div>
            </div>
            <a href="/logout" style="color:var(--gold);"><i class="fas fa-power-off"></i></a>
        </div>
    </div>
    {% endif %}

    <div class="main">
        {{ CONTENT }}
    </div>
</body>
</html>
"""

# --- DATABASE ENGINE ---
class DbContext:
    def __init__(self): self.conn = None
    async def __aenter__(self):
        self.conn = await aiosqlite.connect('tracker.db')
        self.conn.row_factory = aiosqlite.Row
        return self.conn
    async def __aexit__(self, exc_type, exc, tb):
        if self.conn: await self.conn.close()

async def get_db(): return DbContext()

async def init_db():
    async with await get_db() as db:
        await db.executescript(DB_SCHEMA)
        # --- AUTO REPAIR ---
        async def add_col(t, c, d):
            try: await db.execute(f"SELECT {c} FROM {t} LIMIT 1")
            except: 
                try: await db.execute(f"ALTER TABLE {t} ADD COLUMN {c} {d}")
                except: pass
        
        await add_col('targets', 'owner_id', 'INTEGER DEFAULT 1')
        await add_col('targets', 'tg_username', 'TEXT')
        await add_col('targets', 'phone', 'TEXT')
        await add_col('targets', 'pic_path', 'TEXT')
        await add_col('targets', 'notes', 'TEXT')
        await add_col('targets', 'notifications', 'BOOLEAN DEFAULT 0')
        await add_col('users', 'max_targets', 'INTEGER DEFAULT 3')
        await add_col('users', 'expiry_date', 'DATETIME')
        await add_col('users', 'created_at', 'DATETIME DEFAULT CURRENT_TIMESTAMP')
        await add_col('logs', 'metadata', 'TEXT')
        await db.commit()
        
        async with db.execute("SELECT * FROM users WHERE username = ?", (ADMIN_USER,)) as c:
            if not await c.fetchone():
                await db.execute("INSERT INTO users (username, password, is_admin, max_targets) VALUES (?, ?, 1, 9999)", (ADMIN_USER, ADMIN_PASS))
                await db.commit()

    async with await get_db() as db:
        async with db.execute("SELECT value FROM settings WHERE key='api_id'") as c:
            r = await c.fetchone()
            if r: global runtime_api_id; runtime_api_id = int(r['value'])
        async with db.execute("SELECT value FROM settings WHERE key='api_hash'") as c:
            r = await c.fetchone()
            if r: global runtime_api_hash; runtime_api_hash = r['value']

async def get_setting(key):
    async with await get_db() as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as c:
            row = await c.fetchone()
            return row['value'] if row else None

async def save_setting(key, value):
    async with await get_db() as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        await db.commit()

# --- HELPERS ---
def now_tz(): return datetime.now(TZ)
def fmt_time(dt_obj):
    if not dt_obj: return "—"
    if isinstance(dt_obj, str):
        try: dt_obj = datetime.fromisoformat(dt_obj)
        except: return dt_obj
    return dt_obj.strftime('%d %b, %H:%M')

def calc_duration(start, end):
    if isinstance(start, str): start = datetime.fromisoformat(start)
    if isinstance(end, str): end = datetime.fromisoformat(end)
    diff = end - start
    ts = int(diff.total_seconds())
    h, r = divmod(ts, 3600); m, s = divmod(r, 60)
    return f"{h}h {m}m" if h > 0 else f"{m}m {s}s"

async def get_heatmap_data(target_id):
    h = [0] * 24
    async with await get_db() as db:
        async with db.execute("SELECT start_time, end_time FROM sessions WHERE target_id=? ORDER BY id DESC LIMIT 100", (target_id,)) as c:
            rows = await c.fetchall()
    for r in rows:
        try:
            s = datetime.fromisoformat(r['start_time']) if isinstance(r['start_time'], str) else r['start_time']
            e = datetime.fromisoformat(r['end_time']) if r['end_time'] else now_tz()
            h[s.hour] += 1
            while s.hour != e.hour: s += timedelta(hours=1); h[s.hour] += 1
        except: pass
    return [min(x, 10) for x in h]

# --- ENGINE ---
class CyberTracker:
    def __init__(self):
        self.client = None
        self.tracking_active = False

    async def start(self):
        async with await get_db() as db:
            async with db.execute("SELECT value FROM settings WHERE key='session_string'") as c:
                row = await c.fetchone()
                session_str = row['value'] if row else None

        if not runtime_api_id or not runtime_api_hash or not session_str: return
        try:
            self.client = TelegramClient(StringSession(session_str), runtime_api_id, runtime_api_hash)
            await self.client.connect()
            if not await self.client.is_user_authorized(): return
            self.tracking_active = True
            asyncio.create_task(self.loop())
        except Exception as e: logger.error(f"Engine: {e}")

    async def loop(self):
        while self.tracking_active:
            try:
                async with await get_db() as db:
                    async with db.execute("SELECT * FROM targets WHERE is_tracking = 1") as c:
                        targets = await c.fetchall()
                    for t in targets:
                        await self.probe_target(db, t)
                        await asyncio.sleep(0.5) 
            except Exception as e: logger.error(f"Loop: {e}")
            await asyncio.sleep(5)

    async def probe_target(self, db, target):
        t_id = target['id']
        tg_val = None
        if target['tg_id'] and target['tg_id'] != 0: tg_val = target['tg_id']
        elif target['phone']: tg_val = target['phone']
        elif target['tg_username']: tg_val = target['tg_username']
        
        if not tg_val: return
        try:
            entity = await self.client.get_entity(tg_val)
            if (not target['tg_id'] or target['tg_id'] == 0) and entity.id:
                await db.execute("UPDATE targets SET tg_id=? WHERE id=?", (entity.id, t_id))
            
            status = 'offline'
            if isinstance(entity.status, UserStatusOnline): status = 'online'
            elif isinstance(entity.status, UserStatusRecently): status = 'recently'
            
            if status != target['last_status']:
                now = now_tz()
                await db.execute("UPDATE targets SET last_status=?, last_seen=? WHERE id=?", (status, now, t_id))
                await db.execute("INSERT INTO logs (target_id, event_type, timestamp) VALUES (?, ?, ?)", (t_id, status.upper(), now))
                
                if status == 'online' and target['notifications']:
                    try: await self.client.send_message('me', f"🚨 **SPECTRE ALERT**\nTarget: {target['display_name']}\nStatus: ONLINE")
                    except: pass

                if status == 'online':
                    await db.execute("INSERT INTO sessions (target_id, status, start_time) VALUES (?, 'ONLINE', ?)", (t_id, now))
                elif target['last_status'] == 'online':
                    async with db.execute("SELECT id, start_time FROM sessions WHERE target_id=? AND end_time IS NULL ORDER BY id DESC LIMIT 1", (t_id,)) as c:
                        open_sess = await c.fetchone()
                    if open_sess:
                        s = datetime.fromisoformat(open_sess['start_time']) if isinstance(open_sess['start_time'], str) else open_sess['start_time']
                        diff = now - s
                        dur = str(timedelta(seconds=int(diff.total_seconds())))
                        await db.execute("UPDATE sessions SET end_time=?, duration=?, status='FINISHED' WHERE id=?", (now, dur, open_sess['id']))
                await db.commit()
        except: pass

cyber_bot = CyberTracker()

# --- ROUTES ---
def login_required(f):
    @wraps(f)
    async def decorated(*args, **kwargs):
        if 'user_id' not in session: return redirect('/login')
        return await f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    async def decorated(*args, **kwargs):
        if not session.get('is_admin'): abort(403)
        return await f(*args, **kwargs)
    return decorated

@app.route('/')
async def index(): return redirect('/dashboard')

@app.route('/login', methods=['GET', 'POST'])
async def login():
    msg = ""
    if request.method == 'POST':
        form = await request.form
        async with await get_db() as db:
            async with db.execute("SELECT * FROM users WHERE username = ? AND password = ?", (form.get('username'), form.get('password'))) as c:
                user = await c.fetchone()
                if user:
                    if user['expiry_date'] and datetime.now() > datetime.fromisoformat(user['expiry_date']):
                        msg = "Access Expired."
                    else:
                        session['user_id'] = user['id']; session['is_admin'] = bool(user['is_admin'])
                        session['username'] = user['username']
                        return redirect('/dashboard')
                if not msg: msg = "Invalid credentials."
    
    content = f"""
    <div class="login-wrap">
        <div class="login-box">
            <h1 style="text-align:center; font-size:2.2rem; margin-bottom:0.5rem; color:var(--accent);">SPECTRE</h1>
            <p style="color:var(--text-muted); margin-bottom:2.5rem; text-align:center; font-style:italic; font-family:'Lora', serif;">Private Intelligence Ledger</p>
            {f'<div style="color:var(--danger); margin-bottom:1rem; text-align:center; font-size:0.8rem; font-family:Cinzel, serif;">{msg}</div>' if msg else ''}
            <form method="POST">
                <div class="form-group"><label>AGENT IDENTITY</label><input name="username" required placeholder="CODENAME"></div>
                <div class="form-group"><label>SECURE PASSPHRASE</label><input type="password" name="password" required></div>
                <button class="btn" style="width:100%; justify-content:center; padding:16px;">AUTHORIZE ENTRY</button>
            </form>
            <div style="margin-top:2.5rem; text-align:center;">
                <a href="/register" style="color:var(--text-muted); font-size:0.7rem; text-decoration:none; font-family:'Cinzel', serif; letter-spacing:0.1em; border-bottom:1px solid var(--gold);">REQUEST NEW IDENTITY</a>
            </div>
        </div>
    </div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content), hide_sidebar=True)

@app.route('/register', methods=['GET', 'POST'])
async def register():
    msg = ""
    if request.method == 'POST':
        f = await request.form
        try:
            async with await get_db() as db:
                await db.execute("INSERT INTO users (username, password, is_admin, max_targets, expiry_date) VALUES (?, ?, 0, 3, ?)", 
                                 (f.get('username'), f.get('password'), datetime.now() + timedelta(days=14)))
                await db.commit()
            return redirect('/login')
        except: msg = "Identity conflict."
    
    content = f"""
    <div class="login-wrap">
        <div class="login-box">
            <h1 style="text-align:center;">INITIALIZE</h1>
            <p style="color:var(--text-muted); margin-bottom:2rem; text-align:center; font-style:italic;">Create your secure dossier.</p>
            {f'<div style="color:var(--danger); margin-bottom:1rem; text-align:center; font-size:0.8rem;">{msg}</div>' if msg else ''}
            <form method="POST">
                <div class="form-group"><label>PROPOSED CODENAME</label><input name="username" required></div>
                <div class="form-group"><label>PASSPHRASE</label><input type="password" name="password" required></div>
                <button class="btn" style="width:100%; justify-content:center; padding:16px;">ESTABLISH PROFILE</button>
            </form>
            <div style="margin-top:2rem; text-align:center;"><a href="/login" style="color:var(--text-muted); font-size:0.7rem; text-decoration:none; font-family:'Cinzel', serif;">RETURN TO LOGIN</a></div>
        </div>
    </div>"""
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content), hide_sidebar=True)

@app.route('/dashboard')
@login_required
async def dashboard():
    uid = session['user_id']; is_admin = session.get('is_admin')
    query = request.args.get('q', '').lower()
    
    async with await get_db() as db:
        if is_admin: q_sql = "SELECT t.*, u.username as owner FROM targets t LEFT JOIN users u ON t.owner_id = u.id ORDER BY t.last_status DESC"
        else: q_sql = "SELECT t.*, 'ME' as owner FROM targets t WHERE owner_id=? ORDER BY t.last_status DESC"
        async with db.execute(q_sql, (uid,) if not is_admin else ()) as c: targets = await c.fetchall()
        async with db.execute("SELECT * FROM users WHERE id=?", (uid,)) as c: me = await c.fetchone()

    if query: targets = [t for t in targets if query in t['display_name'].lower() or (t['tg_username'] and query in t['tg_username'].lower())]
    
    online_count = sum(1 for t in targets if t['last_status'] == 'online')
    total_count = len(targets)
    
    rows = ""
    for t in targets:
        badge = "on" if t['last_status'] == 'online' else "off"
        ident = t['tg_id'] or t['phone'] or t['tg_username']
        alert = '<i class="fas fa-bell" style="color:var(--gold); font-size:0.7rem;"></i>' if t['notifications'] else ''
        
        rows += f"""
        <tr style="cursor:pointer; transition:background 0.2s;" onclick="window.location='/target/{t['id']}'">
            <td>
                <div style="font-weight:600; font-size:1rem; color:var(--text-main); font-family:'Cormorant Garamond', serif;">{t['display_name']} {alert}</div>
                <div class="mono" style="font-size:0.8rem; color:#999;">{ident}</div>
            </td>
            <td><span class="status-badge {badge}">{t['last_status']}</span></td>
            <td class="mono">{fmt_time(t['last_seen'])}</td>
            <td style="text-align:right;"><i class="fas fa-chevron-right" style="color:#d8d4c8;"></i></td>
        </tr>
        """
    if not rows: rows = "<tr><td colspan='4' style='text-align:center; padding:4rem; color:var(--text-muted); font-style:italic;'>No active intelligence found in the ledger.</td></tr>"

    content = f"""
    <div style="display:flex; justify-content:space-between; align-items:flex-end; margin-bottom:3rem;">
        <div><h1>Mission Control</h1><div class="subtitle" style="margin-bottom:0;">SURVEILLANCE OPERATIONS OVERVIEW</div></div>
        <a href="/add" class="btn"><i class="fas fa-plus"></i> NEW TARGET</a>
    </div>
    
    <div class="stats-grid">
        <div class="stat-card"><div class="stat-label">TOTAL TARGETS</div><div class="stat-val">{total_count}</div><div style="font-size:0.75rem; color:var(--text-muted); font-style:italic;">QUOTA: {me['max_targets']} ALLOWED</div></div>
        <div class="stat-card"><div class="stat-label">ACTIVE UPLINKS</div><div class="stat-val" style="color:var(--success);">{online_count}</div><div style="font-size:0.75rem; color:var(--text-muted); font-style:italic;">CURRENTLY ONLINE</div></div>
        <div class="stat-card"><div class="stat-label">SYSTEM STATUS</div><div class="stat-val" style="font-size:1.8rem; margin:1rem 0;">{'OPERATIONAL' if cyber_bot.tracking_active else 'OFFLINE'}</div>{f'<a href="/connect" style="font-size:0.7rem; text-decoration:none; font-weight:700; color:var(--accent); border-bottom:1px solid;">RECONNECT &rarr;</a>' if not cyber_bot.tracking_active else '<span style="font-size:0.7rem; color:var(--success); font-weight:700;">● MONITORING ACTIVE</span>'}</div>
    </div>

    <div class="table-container">
        <div style="padding:1.5rem; display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--border);">
            <div style="font-family:'Cinzel', serif; font-weight:700; font-size:0.8rem; letter-spacing:0.1em; color:var(--accent);">TARGET LEDGER</div>
            <div class="search-bar"><i class="fas fa-search"></i><form style="margin:0;"><input name="q" placeholder="SEARCH DOSSIERS..." value="{query}"></form></div>
        </div>
        <table><tr><th>IDENTITY</th><th>STATUS</th><th>LAST SEEN</th><th></th></tr>{rows}</table>
    </div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content), active_page='dashboard')

# --- INTELLIGENCE ---
@app.route('/intelligence', methods=['GET', 'POST'])
@login_required
async def intelligence():
    uid = session['user_id']
    async with await get_db() as db:
        async with db.execute("SELECT id, display_name FROM targets WHERE owner_id=?", (uid,)) as c: my_targets = await c.fetchall()
    
    result_html = ""
    if request.method == 'POST':
        t1, t2 = request.form.get('t1'), request.form.get('t2')
        if t1 and t2:
            async with await get_db() as db:
                async with db.execute("SELECT start_time, end_time FROM sessions WHERE target_id=? ORDER BY id DESC LIMIT 50", (t1,)) as c: s1 = await c.fetchall()
                async with db.execute("SELECT start_time, end_time FROM sessions WHERE target_id=? ORDER BY id DESC LIMIT 50", (t2,)) as c: s2 = await c.fetchall()
            
            overlaps = 0
            for a in s1:
                for b in s2:
                    try:
                        a_s, a_e = datetime.fromisoformat(a[0]), datetime.fromisoformat(a[1]) if a[1] else now_tz()
                        b_s, b_e = datetime.fromisoformat(b[0]), datetime.fromisoformat(b[1]) if b[1] else now_tz()
                        if max(a_s, b_s) < min(a_e, b_e): overlaps += 1
                    except: pass
            
            prob = min(100, overlaps * 10)
            result_html = f"""<div class="stat-card" style="border-left:4px solid var(--gold); margin-top:2rem; animation: slideUp 0.6s var(--ease);">
                <h3 style="margin:0; font-family:'Cinzel', serif;">CORRELATION REPORT</h3>
                <div style="display:flex; gap:30px; margin-top:1.5rem;">
                    <div><div class="stat-label">INTERSECTIONS</div><div class="stat-val">{overlaps}</div></div>
                    <div><div class="stat-label">PROBABILITY</div><div class="stat-val">{prob}%</div></div>
                </div></div>"""

    opts = "".join([f"<option value='{t['id']}'>{t['display_name']}</option>" for t in my_targets])
    content = f"""<h1>Co-Incidence Engine</h1><div class="subtitle">Analyze behavioral intersections between subjects.</div>
    <div class="stat-card">
        <form method="POST" style="display:flex; gap:1rem; align-items:flex-end;">
            <div style="flex:1;"><label>SUBJECT A</label><select name="t1">{opts}</select></div>
            <div style="flex:1;"><label>SUBJECT B</label><select name="t2">{opts}</select></div>
            <button class="btn" style="height:50px;">RUN ANALYSIS</button>
        </form>
    </div>
    {result_html}"""
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content), active_page='intelligence')

@app.route('/settings', methods=['GET', 'POST'])
@login_required
async def settings():
    msg = ""
    if request.method == 'POST':
        f = await request.form
        global runtime_api_id, runtime_api_hash
        if f.get('api_id') and f.get('api_hash'):
            runtime_api_id = int(f.get('api_id')); runtime_api_hash = f.get('api_hash')
            async with await get_db() as db:
                await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('api_id', ?)", (str(runtime_api_id),))
                await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('api_hash', ?)", (runtime_api_hash,))
                await db.commit()
            msg = "CONFIGURATION UPDATED."
    
    content = f"""
    <h1>Configuration</h1><div class="subtitle">SYSTEM PARAMETERS.</div>
    <div style="display:grid; grid-template-columns: 2fr 1fr; gap:2rem;">
        <div class="stat-card">
            <h2 style="margin-top:0;">Telegram API</h2>
            <p style="color:var(--text-muted); font-size:0.85rem; margin-bottom:1.5rem; font-style:italic;">Credentials required for network access.</p>
            {f'<div style="color:var(--success); margin-bottom:1rem; font-size:0.8rem; font-weight:600;">{msg}</div>' if msg else ''}
            <form method="POST">
                <div class="form-group"><label>API ID</label><input name="api_id" value="{runtime_api_id if runtime_api_id else ''}"></div>
                <div class="form-group"><label>API HASH</label><input name="api_hash" value="{runtime_api_hash if runtime_api_hash else ''}"></div>
                <button class="btn">SAVE CONFIGURATION</button>
            </form>
        </div>
        <div class="stat-card">
            <h2 style="margin-top:0;">Status</h2>
            <div style="margin-top:1.5rem;">
                <div style="font-weight:600; font-size:0.8rem; color:var(--text-muted); font-family:'Cinzel', serif;">UPLINK STATE</div>
                <div style="font-size:1.5rem; font-weight:500; color:{'var(--success)' if cyber_bot.tracking_active else 'var(--danger)'}; margin:0.5rem 0; font-family:'Cormorant Garamond', serif;">{'CONNECTED' if cyber_bot.tracking_active else 'DISCONNECTED'}</div>
                <a href="/connect" class="btn btn-outline" style="width:100%; justify-content:center;">RESET UPLINK</a>
            </div>
        </div>
    </div>"""
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content), active_page='settings')

@app.route('/target/<int:t_id>')
@login_required
async def target_detail(t_id):
    async with await get_db() as db:
        async with db.execute("SELECT * FROM targets WHERE id=?", (t_id,)) as c: t = await c.fetchone()
        async with db.execute("SELECT * FROM sessions WHERE target_id=? ORDER BY id DESC LIMIT 15", (t_id,)) as c: s = await c.fetchall()
    
    if not t or (not session.get('is_admin') and t['owner_id'] != session['user_id']): return "ACCESS DENIED"
    
    if request.args.get('toggle_alert'):
        async with await get_db() as db:
            await db.execute("UPDATE targets SET notifications=? WHERE id=?", (0 if t['notifications'] else 1, t_id)); await db.commit()
        return redirect(url_for('target_detail', t_id=t_id))

    sess_rows = "".join([f"<tr><td class='mono'>{fmt_time(x['start_time'])}</td><td class='mono'>{fmt_time(x['end_time'])}</td><td class='mono' style='color:var(--text-main); font-style:normal;'>{x['duration'] or 'ACTIVE'}</td></tr>" for x in s])
    alert_btn = f"""<a href="?toggle_alert=1" class="btn btn-outline" style="color:{'var(--success)' if t['notifications'] else 'var(--text-main)'}; border-color:{'var(--success)' if t['notifications'] else 'var(--border)'};"><i class="fas fa-bell"></i> {'NOTIFICATIONS ON' if t['notifications'] else 'ENABLE ALERTS'}</a>"""

    content = f"""
    <div style="display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:2rem;">
        <div>
            <a href="/dashboard" style="color:var(--text-muted); text-decoration:none; font-size:0.75rem; font-weight:600; letter-spacing:0.05em; font-family:'Cinzel', serif;">&larr; BACK TO DASHBOARD</a>
            <h1 style="margin-top:1rem;">{t['display_name']}</h1>
            <div class="mono" style="color:var(--text-muted); margin-top:0.5rem; font-style:normal;">ID: {t['tg_id'] or t['phone'] or t['tg_username']}</div>
        </div>
        <div style="display:flex; gap:10px; margin-top:1rem;">
            {alert_btn}
            <a href="/export/{t_id}" class="btn btn-outline">EXPORT</a>
            <a href="/delete/{t_id}" class="btn" style="background:#fff; border-color:#e5e7eb; color:var(--danger);">DELETE</a>
        </div>
    </div>
    <div style="display:grid; grid-template-columns: 2fr 1fr; gap:2rem;">
        <div class="table-container">
            <div style="padding:1.5rem; border-bottom:1px solid var(--border); font-family:'Cinzel', serif; font-weight:700; font-size:0.8rem; color:var(--accent);">SESSION LOG</div>
            <table><tr><th>START TIME</th><th>END TIME</th><th>DURATION</th></tr>{sess_rows}</table>
        </div>
        <div class="stat-card">
            <h2 style="margin-top:0;">Notes</h2>
            <textarea style="height:150px; margin-bottom:1rem; resize:none;" placeholder="Add intelligence notes..."></textarea>
            <button class="btn btn-outline" style="width:100%; justify-content:center;">SAVE</button>
        </div>
    </div>"""
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content), active_page='dashboard')

@app.route('/add', methods=['GET', 'POST'])
@login_required
async def add():
    if request.method == 'POST':
        f = await request.form
        uid = session['user_id']
        tid = int(f.get('user_id')) if f.get('user_id') and f.get('user_id').isdigit() else 0
        async with await get_db() as db:
            await db.execute("INSERT INTO targets (owner_id, tg_id, tg_username, phone, display_name, last_status) VALUES (?,?,?,?,?, 'unknown')", 
                             (uid, tid, f.get('username'), f.get('phone'), f.get('name')))
            await db.commit()
        return redirect('/dashboard')

    content = """
    <div style="max-width:600px; margin:auto; animation: slideUp 0.6s var(--ease);">
        <h1>Initialize Target.</h1>
        <div class="subtitle">PROVIDE AT LEAST ONE IDENTIFIER TO BEGIN SURVEILLANCE.</div>
        <div class="stat-card" style="margin-top:2rem;">
            <form method="POST">
                <div class="form-group"><label>TARGET DESIGNATION (NAME)</label><input name="name" required placeholder="SUBJECT ALPHA"></div>
                <div style="border-top:1px solid var(--border); margin: 2rem 0; position:relative;"><span style="position:absolute; top:-10px; left:0; background:var(--surface); padding-right:10px; font-size:0.7rem; color:var(--text-muted); font-weight:600; font-family:'Cinzel', serif;">IDENTIFIERS</span></div>
                <div style="display:grid; grid-template-columns: 1fr 1fr; gap:1.5rem;">
                    <div class="form-group"><label style="color:var(--success);">PRIORITY 1: NUMERIC ID</label><input name="user_id" placeholder="123456789"></div>
                    <div class="form-group"><label style="color:var(--success);">PRIORITY 1: PHONE NUMBER</label><input name="phone" placeholder="+1..."></div>
                </div>
                <div class="form-group"><label style="color:var(--text-main);">PRIORITY 2: USERNAME</label><input name="username" placeholder="@target_handle"></div>
                <button class="btn" style="width:100%; justify-content:center; margin-top:1rem; padding:16px;">BEGIN SURVEILLANCE</button>
            </form>
        </div>
    </div>"""
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content), active_page='dashboard')

# --- LOGS & USERS ---
@app.route('/logs')
@login_required
async def logs():
    async with await get_db() as db:
        q = "SELECT l.*, t.display_name FROM logs l LEFT JOIN targets t ON l.target_id = t.id ORDER BY l.timestamp DESC LIMIT 100"
        async with db.execute(q) as c: logs = await c.fetchall()
    rows = "".join([f"<tr><td class='mono'>{fmt_time(l['timestamp'])}</td><td>{l['display_name'] or 'SYSTEM'}</td><td style='font-weight:600; color:{'var(--success)' if l['event_type'] == 'ONLINE' else 'var(--text-muted)'}'>{l['event_type']}</td></tr>" for l in logs])
    content = f"<h1>Audit Logs</h1><div class='subtitle'>SYSTEM ACTIVITY ARCHIVE.</div><div class='table-container'><table><tr><th>TIME</th><th>ENTITY</th><th>EVENT</th></tr>{rows}</table></div>"
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content), active_page='logs')

@app.route('/users', methods=['GET', 'POST'])
@login_required
@admin_required
async def users():
    if request.method == 'POST':
        f = await request.form
        if 'delete' in f:
            async with await get_db() as db: await db.execute("DELETE FROM users WHERE id=?", (f['delete'],)); await db.commit()
        else:
            try:
                async with await get_db() as db:
                    await db.execute("INSERT INTO users (username, password, is_admin, max_targets) VALUES (?, ?, ?, ?)", 
                                     (f['username'], f['password'], 1 if f.get('role')=='admin' else 0, int(f['max_targets'])))
                    await db.commit()
            except: pass
    async with await get_db() as db:
        async with db.execute("SELECT * FROM users") as c: users = await c.fetchall()
    rows = "".join([f"<tr><td>{u['username']}</td><td>{'ADMIN' if u['is_admin'] else 'USER'}</td><td>{u['max_targets']}</td><td>{f'<form method=POST style=display:inline><input type=hidden name=delete value={u['id']}><button class=btn-outline style=padding:4px;color:var(--danger)>REMOVE</button></form>' if u['id'] != session['user_id'] else ''}</td></tr>" for u in users])
    content = f"<h1>User Management</h1><div class='subtitle'>ACCESS CONTROL.</div><div style='display:grid; grid-template-columns: 2fr 1fr; gap:2rem;'><div class='table-container'><table><tr><th>IDENTITY</th><th>ROLE</th><th>QUOTA</th><th>ACTION</th></tr>{rows}</table></div><div class='stat-card'><h2 style='margin-top:0;'>Provision</h2><form method='POST'><div class='form-group'><label>USER</label><input name='username' required></div><div class='form-group'><label>PASS</label><input name='password' required></div><div class='form-group'><label>ROLE</label><select name='role'><option value='user'>USER</option><option value='admin'>ADMIN</option></select></div><div class='form-group'><label>QUOTA</label><input name='max_targets' type='number' value='5'></div><button class='btn' style='width:100%;'>CREATE</button></form></div></div>"
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content), active_page='users')

@app.route('/connect', methods=['GET', 'POST'])
@login_required
async def connect():
    if request.method == 'POST':
        f = await request.form
        global runtime_api_id, runtime_api_hash, temp_client, phone_number, phone_code_hash
        runtime_api_id = int(f.get('api_id')); runtime_api_hash = f.get('api_hash')
        await save_setting('api_id', runtime_api_id); await save_setting('api_hash', runtime_api_hash)
        try:
            temp_client = TelegramClient(StringSession(), runtime_api_id, runtime_api_hash)
            await temp_client.connect(); phone_number = f.get('phone'); send = await temp_client.send_code_request(phone_number); phone_code_hash = send.phone_code_hash; return redirect('/verify')
        except Exception as e: return f"Error: {e}"
    content = """<div class="stat-card" style="max-width:500px; margin:auto; margin-top:2rem;"><h2>Establish Connection</h2><form method="POST"><div class="form-group"><label>API ID</label><input name="api_id" required></div><div class="form-group"><label>API Hash</label><input name="api_hash" required></div><div class="form-group"><label>Phone</label><input name="phone" required></div><button class="btn" style="width:100%;">Request OTP</button></form></div>"""
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content), active_page='settings')

@app.route('/verify', methods=['GET', 'POST'])
@login_required
async def verify():
    if request.method == 'POST':
        try:
            await temp_client.sign_in(phone=phone_number, code=(await request.form).get('code'), phone_code_hash=phone_code_hash)
            await save_setting('session_string', temp_client.session.save())
            await temp_client.disconnect(); asyncio.create_task(cyber_bot.start()); return redirect('/dashboard')
        except: return "Invalid OTP"
    content = """<div class="stat-card" style="max-width:400px; margin:auto; margin-top:2rem;"><h2>Verify Identity</h2><form method="POST"><div class="form-group"><label>OTP Code</label><input name="code" required></div><button class="btn" style="width:100%;">Confirm</button></form></div>"""
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content), active_page='settings')

@app.route('/delete/<int:t_id>')
@login_required
async def delete(t_id):
    async with await get_db() as db: await db.execute("DELETE FROM targets WHERE id=?", (t_id,)); await db.commit()
    return redirect('/dashboard')

@app.route('/export/<int:t_id>')
@login_required
async def export(t_id):
    async with await get_db() as db:
        async with db.execute("SELECT * FROM sessions WHERE target_id=? ORDER BY id DESC", (t_id,)) as c: rows = await c.fetchall()
    si = io.StringIO(); cw = csv.writer(si); cw.writerow(['Status', 'Start', 'End', 'Duration'])
    for r in rows: cw.writerow([r['status'], r['start_time'], r['end_time'], r['duration']])
    return Response(si.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=log_{t_id}.csv"})

@app.route('/logout')
async def logout(): session.clear(); return redirect('/login')

@app.before_serving
async def startup():
    await init_db(); asyncio.create_task(cyber_bot.start())

if __name__ == "__main__":
    config = Config(); config.bind = [f"0.0.0.0:{os.getenv('PORT', '8000')}"]; asyncio.run(serve(app, config))
