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
logger = logging.getLogger("Spectre.Editorial")

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
    expiry_date DATETIME
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
    pic_path TEXT
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
    timestamp DATETIME
);
"""

# --- UI TEMPLATE (ELLIPSUS / EDITORIAL THEME) ---
HTML_BASE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SPECTRE // Intelligence</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;600;700&family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        :root {
            --bg: #f9f9f9; /* Editorial Off-White */
            --surface: #ffffff;
            --text-main: #111111;
            --text-sec: #555555;
            --border: #e0e0e0;
            --accent: #000000;
            --success: #059669; /* Muted Emerald */
            --error: #dc2626;   /* Muted Red */
        }
        
        * { box-sizing: border-box; -webkit-font-smoothing: antialiased; }
        
        body { 
            background: var(--bg); 
            color: var(--text-main); 
            font-family: 'Inter', sans-serif; 
            margin: 0; 
            padding-bottom: 50px;
            line-height: 1.5;
        }

        h1, h2, h3, .brand { 
            font-family: 'Playfair Display', serif; 
            color: var(--accent); 
            font-weight: 600;
            margin-top: 0;
        }

        /* NAVIGATION */
        .nav {
            background: var(--surface);
            border-bottom: 1px solid var(--border);
            padding: 1.5rem 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            position: sticky; top: 0; z-index: 100;
        }
        .brand { font-size: 1.5rem; text-decoration: none; letter-spacing: -0.02em; }
        .nav-links a {
            font-family: 'Inter', sans-serif;
            font-size: 0.85rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-sec);
            text-decoration: none;
            margin-left: 2rem;
            transition: color 0.2s;
            font-weight: 500;
        }
        .nav-links a:hover { color: var(--accent); }

        /* LAYOUT */
        .container { max-width: 1000px; margin: 3rem auto; width: 92%; }
        
        /* CARDS */
        .card {
            background: var(--surface);
            border: 1px solid var(--border);
            padding: 2.5rem;
            margin-bottom: 1.5rem;
            border-radius: 2px; /* Sharp professional corners */
            box-shadow: 0 1px 3px rgba(0,0,0,0.02);
        }

        /* FORMS & INPUTS - The "Professional" Look */
        label {
            display: block;
            font-family: 'Inter', sans-serif;
            font-size: 0.7rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--text-sec);
            margin-bottom: 0.6rem;
            margin-top: 1.2rem;
        }
        
        input, select {
            width: 100%;
            padding: 14px 16px;
            border: 1px solid var(--border);
            background: #fff;
            border-radius: 0; /* No rounded corners for pro look */
            font-family: 'Inter', sans-serif;
            font-size: 0.95rem;
            color: var(--text-main);
            transition: border 0.2s;
        }
        input:focus { outline: none; border-color: var(--accent); }
        input::placeholder { color: #ccc; }

        /* BUTTONS */
        button, .btn {
            display: inline-block;
            background: var(--accent);
            color: #fff;
            border: 1px solid var(--accent);
            padding: 14px 28px;
            font-family: 'Inter', sans-serif;
            font-size: 0.8rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            cursor: pointer;
            text-decoration: none;
            transition: all 0.2s;
            text-align: center;
            border-radius: 0; /* Professional sharp edges */
            margin-top: 1rem;
        }
        button:hover, .btn:hover { background: #333; border-color: #333; }
        
        .btn-ghost {
            background: transparent;
            color: var(--text-sec);
            border: 1px solid var(--border);
        }
        .btn-ghost:hover { border-color: var(--accent); color: var(--accent); background: transparent; }

        /* GRID SYSTEM */
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 1.5rem; }
        
        .target-item {
            background: var(--surface);
            border: 1px solid var(--border);
            padding: 1.5rem;
            cursor: pointer;
            transition: border 0.2s, box-shadow 0.2s;
            display: flex; justify-content: space-between; align-items: flex-start;
        }
        .target-item:hover { border-color: #999; box-shadow: 0 4px 12px rgba(0,0,0,0.04); }
        
        .status-indicator {
            font-size: 0.7rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            padding-left: 10px;
            border-left: 2px solid transparent;
        }
        .s-online { color: var(--success); border-left-color: var(--success); }
        .s-offline { color: #999; border-left-color: #ccc; }

        .mono-id { font-family: 'JetBrains Mono', monospace; font-size: 0.75rem; color: #777; margin-top: 0.5rem; }

        /* TABLES */
        table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
        th { text-align: left; font-size: 0.7rem; text-transform: uppercase; color: #777; padding: 12px; border-bottom: 2px solid #eee; letter-spacing: 0.05em; }
        td { padding: 14px 12px; border-bottom: 1px solid #f0f0f0; font-size: 0.9rem; }
        
        /* UTILS */
        .header-flex { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 2rem; border-bottom: 1px solid #eee; padding-bottom: 1rem; }
        .alert { background: #fee2e2; color: #991b1b; padding: 1rem; border: 1px solid #fecaca; margin-bottom: 1rem; font-size: 0.9rem; }
    </style>
</head>
<body>
    <nav class="nav">
        <a href="/dashboard" class="brand">SPECTRE.</a>
        <div class="nav-links">
            {% if session.get('is_admin') %}
            <a href="/users">ACCESS CONTROL</a>
            {% endif %}
            <a href="/logs">ARCHIVE</a>
            <a href="/logout" style="color:var(--error);">TERMINATE</a>
        </div>
    </nav>
    <div class="container">
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
        # --- AUTO-REPAIR COLUMNS ---
        async def check_col(t, c, d):
            try: await db.execute(f"SELECT {c} FROM {t} LIMIT 1")
            except: 
                logger.info(f"Adding column {c} to {t}")
                try: await db.execute(f"ALTER TABLE {t} ADD COLUMN {c} {d}")
                except: pass
        
        await check_col('targets', 'owner_id', 'INTEGER DEFAULT 1')
        await check_col('targets', 'tg_username', 'TEXT')
        await check_col('targets', 'phone', 'TEXT')
        await check_col('targets', 'pic_path', 'TEXT')
        await check_col('users', 'max_targets', 'INTEGER DEFAULT 3')
        await check_col('users', 'expiry_date', 'DATETIME')
        await db.commit()
        
        # Admin
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
    return dt_obj.strftime('%b %d, %H:%M')

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
        session_str = await get_setting('session_string')
        if not runtime_api_id or not runtime_api_hash or not session_str: return
        try:
            self.client = TelegramClient(StringSession(session_str), runtime_api_id, runtime_api_hash)
            await self.client.connect()
            if not await self.client.is_user_authorized(): return
            self.tracking_active = True
            asyncio.create_task(self.loop())
        except Exception as e: logger.error(f"Init Error: {e}")

    async def loop(self):
        while self.tracking_active:
            try:
                async with await get_db() as db:
                    async with db.execute("SELECT * FROM targets WHERE is_tracking = 1") as c: targets = await c.fetchall()
                    for t in targets: await self.probe_target(db, t); await asyncio.sleep(0.5)
            except Exception as e: logger.error(f"Loop: {e}")
            await asyncio.sleep(4)

    async def probe_target(self, db, target):
        t_id = target['id']
        # Priority Logic: ID > Phone > Username
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
                if status == 'online':
                    await db.execute("INSERT INTO sessions (target_id, status, start_time) VALUES (?, 'ONLINE', ?)", (t_id, now))
                elif target['last_status'] == 'online':
                    async with db.execute("SELECT id, start_time FROM sessions WHERE target_id=? AND end_time IS NULL ORDER BY id DESC LIMIT 1", (t_id,)) as c:
                        open_sess = await c.fetchone()
                    if open_sess:
                        dur = calc_duration(open_sess['start_time'], now)
                        await db.execute("UPDATE sessions SET end_time=?, duration=?, status='FINISHED' WHERE id=?", (now, dur, open_sess['id']))
                await db.commit()
        except: pass

cyber_bot = CyberTracker()

# --- WEB ROUTES ---
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
                        msg = "LICENSE EXPIRED. CONTACT ADMINISTRATOR."
                    else:
                        session['user_id'] = user['id']; session['is_admin'] = bool(user['is_admin'])
                        return redirect('/dashboard')
                if not msg: msg = "CREDENTIALS REJECTED."
    content = f"""
    <div style="max-width:400px; margin: 80px auto;">
        <h1 style="text-align:center; font-size:2.5rem; margin-bottom:2rem;">Spectre.</h1>
        <div class="card">
            <h2 style="font-size:1.2rem; margin-bottom:1.5rem;">AUTHORIZE SESSION</h2>
            {f'<div class="alert">{msg}</div>' if msg else ''}
            <form method="POST">
                <label>AGENT ID</label><input name="username" required>
                <label>PASSPHRASE</label><input type="password" name="password" required>
                <button type="submit" style="width:100%;">AUTHENTICATE</button>
            </form>
            <div style="text-align:center; margin-top:2rem;">
                <a href="/register" style="color:#666; font-size:0.75rem; text-decoration:none; font-weight:600;">INITIALIZE NEW ACCOUNT &rarr;</a>
            </div>
        </div>
    </div>"""
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/register', methods=['GET', 'POST'])
async def register():
    msg = ""
    if request.method == 'POST':
        f = await request.form
        try:
            async with await get_db() as db:
                # 14 Day Trial Logic (Hidden)
                await db.execute("INSERT INTO users (username, password, is_admin, max_targets, expiry_date) VALUES (?, ?, 0, 3, ?)", 
                                 (f.get('username'), f.get('password'), datetime.now() + timedelta(days=14)))
                await db.commit()
            return redirect('/login')
        except: msg = "USERNAME UNAVAILABLE."
    content = f"""
    <div style="max-width:400px; margin: 80px auto;">
        <h1 style="text-align:center; font-size:2.5rem; margin-bottom:2rem;">Spectre.</h1>
        <div class="card">
            <h2 style="font-size:1.2rem; margin-bottom:1.5rem;">NEW AGENT INITIALIZATION</h2>
            {f'<div class="alert">{msg}</div>' if msg else ''}
            <form method="POST">
                <label>DESIRED CODENAME</label><input name="username" required>
                <label>SECURE PASSPHRASE</label><input type="password" name="password" required>
                <button type="submit" style="width:100%;">CREATE PROFILE</button>
            </form>
            <div style="margin-top:1.5rem; text-align:center;">
                <a href="/login" style="font-size:0.75rem; color:#666; font-weight:600; text-decoration:none;">&larr; RETURN TO LOGIN</a>
            </div>
        </div>
    </div>"""
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/dashboard')
@login_required
async def dashboard():
    uid = session['user_id']; is_admin = session.get('is_admin')
    async with await get_db() as db:
        async with db.execute("SELECT * FROM users WHERE id=?", (uid,)) as c: user = await c.fetchone()
        if is_admin:
            q = "SELECT t.*, u.username as owner FROM targets t LEFT JOIN users u ON t.owner_id = u.id ORDER BY t.last_status DESC"
            args = ()
        else:
            q = "SELECT t.*, 'ME' as owner FROM targets t WHERE owner_id=? ORDER BY t.last_status DESC"
            args = (uid,)
        async with db.execute(q, args) as c: targets = await c.fetchall()
    
    my_targets = [t for t in targets if t['owner_id'] == uid]
    usage = f"{len(my_targets)} / {user['max_targets']}"
    
    rows = ""
    for t in targets:
        s_cls = "s-online" if t['last_status'] == 'online' else "s-offline"
        rows += f"""
        <div class="target-item" onclick="location.href='/target/{t['id']}'">
            <div>
                <div style="font-weight:600; font-size:1rem;">{t['display_name']}</div>
                <div class="mono-id">{t['tg_id'] or t['phone'] or t['tg_username']}</div>
            </div>
            <div class="status-indicator {s_cls}">{t['last_status']}</div>
        </div>"""

    if not rows: rows = "<div style='grid-column:1/-1; text-align:center; padding:3rem; color:#999;'>NO ACTIVE SURVEILLANCE TARGETS.</div>"

    content = f"""
    <div class="header-flex">
        <div>
            <h1 style="font-size:3rem; margin-bottom:0.5rem;">Nexus.</h1>
            <div style="font-family:'Inter'; font-size:0.85rem; color:#666; font-weight:500;">
                QUOTA UTILIZATION: <strong>{usage}</strong>
            </div>
        </div>
        <a href="/add" class="btn">INITIALIZE NEW TARGET</a>
    </div>
    
    {f'<div class="alert">⚠️ UPLINK OFFLINE. <a href="/connect" style="color:inherit; font-weight:bold;">RECONNECT TELEGRAM</a></div>' if not cyber_bot.tracking_active else ''}
    
    <div class="grid">{rows}</div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/target/<int:t_id>')
@login_required
async def target_detail(t_id):
    async with await get_db() as db:
        async with db.execute("SELECT * FROM targets WHERE id=?", (t_id,)) as c: target = await c.fetchone()
        async with db.execute("SELECT * FROM sessions WHERE target_id=? ORDER BY id DESC LIMIT 10", (t_id,)) as c: sess = await c.fetchall()
    
    if not target or (not session.get('is_admin') and target['owner_id'] != session['user_id']): return "ACCESS DENIED"
    h_data = await get_heatmap_data(t_id)
    
    sess_rows = "".join([f"<tr><td>{fmt_time(s['start_time'])}</td><td>{fmt_time(s['end_time'])}</td><td style='font-family:monospace;'>{s['duration'] or 'ACTIVE'}</td></tr>" for s in sess])
    
    content = f"""
    <div style="margin-bottom:2rem;">
        <a href="/dashboard" style="color:#666; text-decoration:none; font-size:0.8rem; font-weight:600;">&larr; BACK TO NEXUS</a>
    </div>
    
    <div class="header-flex">
        <div>
            <h1 style="font-size:2.5rem;">{target['display_name']}</h1>
            <div class="mono-id" style="font-size:0.9rem;">ID: {target['tg_id'] or 'PENDING'}</div>
        </div>
        <div>
            <a href="/export/{t_id}" class="btn btn-ghost">EXPORT LOGS</a>
            <a href="/delete/{t_id}" class="btn" style="background:#fff; border-color:#d1d5db; color:#ef4444;">TERMINATE</a>
        </div>
    </div>

    <div style="display:grid; grid-template-columns: 2fr 1fr; gap:2rem;">
        <div class="card">
            <h3 style="margin-bottom:1.5rem; font-size:1.1rem;">ACTIVITY PATTERN (24H)</h3>
            <div style="height:200px;"><canvas id="heatmap"></canvas></div>
        </div>
        <div class="card">
            <h3 style="margin-bottom:1.5rem; font-size:1.1rem;">RECENT SESSIONS</h3>
            <table>
                <tr><th>START</th><th>END</th><th>DURATION</th></tr>
                {sess_rows}
            </table>
        </div>
    </div>
    <script>
    new Chart(document.getElementById('heatmap'), {{
        type: 'bar',
        data: {{ labels: Array.from({{length:24}},(_,i)=>i+":00"), datasets: [{{ data: {h_data}, backgroundColor: '#111', borderRadius: 0 }}] }},
        options: {{ responsive: true, maintainAspectRatio: false, scales: {{ x: {{ grid: {{ display: false }} }}, y: {{ display: false }} }}, plugins: {{ legend: {{ display: false }} }} }}
    }});
    </script>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/add', methods=['GET', 'POST'])
@login_required
async def add():
    uid = session['user_id']
    async with await get_db() as db:
        async with db.execute("SELECT max_targets FROM users WHERE id=?", (uid,)) as c: u = await c.fetchone()
        async with db.execute("SELECT COUNT(*) as c FROM targets WHERE owner_id=?", (uid,)) as c: cnt = (await c.fetchone())['c']
    if cnt >= u['max_targets']: return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', '<div class="container"><div class="alert">QUOTA LIMIT REACHED. CONTACT ADMIN TO UPGRADE.</div><a href="/dashboard">Back</a></div>'))

    if request.method == 'POST':
        f = await request.form
        # Priority Logic inside Route
        tid = int(f.get('user_id')) if f.get('user_id') and f.get('user_id').isdigit() else 0
        async with await get_db() as db:
            await db.execute("INSERT INTO targets (owner_id, tg_id, tg_username, phone, display_name, last_status) VALUES (?,?,?,?,?, 'unknown')", 
                             (uid, tid, f.get('username'), f.get('phone'), f.get('name')))
            await db.commit()
        return redirect('/dashboard')

    content = """
    <div style="max-width:500px; margin:auto;">
        <h1 style="margin-bottom:1rem;">Initialize Target.</h1>
        <p style="color:#666; margin-bottom:2rem; font-size:0.9rem;">Provide at least one identifier to begin surveillance.</p>
        <div class="card">
            <form method="POST">
                <div class="field">
                    <label>TARGET ALIAS (REQUIRED)</label>
                    <input name="name" required placeholder="Ex: Project Alpha">
                </div>
                
                <div style="border-top:1px solid #eee; margin: 1.5rem 0;"></div>
                
                <div class="field">
                    <label style="color:var(--success);">PRIORITY 1: NUMERIC ID</label>
                    <input name="user_id" placeholder="123456789">
                </div>
                <div class="field">
                    <label style="color:var(--success);">PRIORITY 1: PHONE NUMBER</label>
                    <input name="phone" placeholder="+91...">
                </div>
                <div class="field">
                    <label style="color:#3b82f6;">PRIORITY 2: USERNAME</label>
                    <input name="username" placeholder="@target_handle">
                </div>
                
                <button type="submit" style="width:100%;">START TRACKING</button>
            </form>
        </div>
        <div style="text-align:center;"><a href="/dashboard" style="color:#999; text-decoration:none; font-size:0.8rem;">CANCEL</a></div>
    </div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/logs')
@login_required
async def view_logs():
    async with await get_db() as db:
        q = "SELECT l.timestamp, l.event_type, t.display_name FROM logs l LEFT JOIN targets t ON l.target_id = t.id ORDER BY l.timestamp DESC LIMIT 100"
        async with db.execute(q) as c: logs = await c.fetchall()
    
    rows = ""
    for l in logs:
        style = "color:var(--success);" if l['event_type'] == 'ONLINE' else "color:#999;"
        rows += f"<tr><td style='font-family:monospace; color:#666;'>{fmt_time(l['timestamp'])}</td><td>{l['display_name'] or 'SYSTEM'}</td><td style='font-weight:600; {style}'>{l['event_type']}</td></tr>"
    
    content = f"""
    <h1 style="margin-bottom:2rem;">System Archive.</h1>
    <div class="card" style="padding:0;">
        <table style="margin:0;">
            <tr><th style="padding-left:1.5rem;">TIMESTAMP</th><th>ENTITY</th><th>EVENT</th></tr>
            {rows}
        </table>
    </div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

# --- CONNECT & VERIFY (Clean UI) ---
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
            await temp_client.connect()
            phone_number = f.get('phone')
            send = await temp_client.send_code_request(phone_number)
            phone_code_hash = send.phone_code_hash
            return redirect('/verify')
        except Exception as e: return f"CONNECTION FAILED: {e}"
    content = """<div class="card" style="max-width:450px; margin:auto;"><h2>ESTABLISH UPLINK</h2><form method="POST"><div class="field"><label>API ID</label><input name="api_id" required></div><div class="field"><label>API HASH</label><input name="api_hash" required></div><div class="field"><label>PHONE</label><input name="phone" required></div><button type="submit" style="width:100%;">SEND VERIFICATION</button></form></div>"""
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/verify', methods=['GET', 'POST'])
@login_required
async def verify():
    if request.method == 'POST':
        try:
            await temp_client.sign_in(phone=phone_number, code=(await request.form).get('code'), phone_code_hash=phone_code_hash)
            await save_setting('session_string', temp_client.session.save())
            await temp_client.disconnect(); asyncio.create_task(cyber_bot.start())
            return redirect('/dashboard')
        except: return "INVALID OTP."
    content = """<div class="card" style="max-width:450px; margin:auto;"><h2>VERIFY IDENTITY</h2><form method="POST"><div class="field"><label>OTP CODE</label><input name="code" required></div><button type="submit" style="width:100%;">CONFIRM</button></form></div>"""
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

# --- ADMIN USER MANAGEMENT ---
@app.route('/users', methods=['GET', 'POST'])
@login_required
@admin_required
async def manage_users():
    msg = ""
    if request.method == 'POST':
        f = await request.form
        if 'delete_id' in f:
            uid = f.get('delete_id')
            if int(uid) == session['user_id']: msg = "CANNOT DELETE ACTIVE ADMIN."
            else:
                async with await get_db() as db:
                    await db.execute("DELETE FROM users WHERE id=?", (uid,)); await db.commit()
        else:
            try:
                async with await get_db() as db:
                    await db.execute("INSERT INTO users (username, password, is_admin, max_targets, expiry_date) VALUES (?, ?, ?, ?, ?)", 
                                     (f.get('username'), f.get('password'), 1 if f.get('role')=='admin' else 0, int(f.get('max_targets')), datetime.now() + timedelta(days=int(f.get('validity')))))
                    await db.commit()
                msg = "USER PROVISIONED."
            except: msg = "USERNAME CONFLICT."
    
    async with await get_db() as db:
        async with db.execute("SELECT * FROM users") as c: users = await c.fetchall()
    
    rows = ""
    for u in users:
        del_btn = f"<form method='POST' style='display:inline;'><input type='hidden' name='delete_id' value='{u['id']}'><button class='btn-ghost' style='padding:5px 10px; font-size:0.7rem; color:var(--error); border-color:var(--error);'>DEL</button></form>" if u['id'] != session['user_id'] else ""
        rows += f"<tr><td>{u['username']}</td><td>{'ADMIN' if u['is_admin'] else 'USER'}</td><td>{u['max_targets']}</td><td>{del_btn}</td></tr>"

    content = f"""
    <div style="display:grid; grid-template-columns: 1fr 2fr; gap:2rem;">
        <div class="card">
            <h3>PROVISION USER</h3>
            <div style="color:var(--error); font-size:0.8rem; margin-bottom:1rem;">{msg}</div>
            <form method="POST">
                <div class="field"><label>USERNAME</label><input name="username" required></div>
                <div class="field"><label>PASSWORD</label><input name="password" required></div>
                <div class="field"><label>ROLE</label><select name="role"><option value="user">USER</option><option value="admin">ADMIN</option></select></div>
                <div class="field"><label>QUOTA</label><input type="number" name="max_targets" value="5"></div>
                <div class="field"><label>VALIDITY (DAYS)</label><input type="number" name="validity" value="30"></div>
                <button type="submit" style="width:100%;">CREATE</button>
            </form>
        </div>
        <div class="card" style="padding:0;">
            <table style="margin:0;">
                <tr><th style="padding-left:1.5rem;">IDENTITY</th><th>ROLE</th><th>LIMIT</th><th>ACTION</th></tr>
                {rows}
            </table>
        </div>
    </div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/delete/<int:t_id>')
@login_required
async def delete_target(t_id):
    async with await get_db() as db:
        async with db.execute("SELECT owner_id FROM targets WHERE id=?", (t_id,)) as c: t = await c.fetchone()
        if t and (t['owner_id'] == session['user_id'] or session.get('is_admin')):
            await db.execute("DELETE FROM targets WHERE id=?", (t_id,)); await db.commit()
    return redirect('/dashboard')

@app.route('/export/<int:t_id>')
@login_required
async def export_csv(t_id):
    async with await get_db() as db:
        async with db.execute("SELECT * FROM sessions WHERE target_id=? ORDER BY id DESC", (t_id,)) as c: rows = await c.fetchall()
    si = io.StringIO(); cw = csv.writer(si)
    cw.writerow(['Status', 'Start', 'End', 'Duration'])
    for r in rows: cw.writerow([r['status'], r['start_time'], r['end_time'], r['duration']])
    return Response(si.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=log_{t_id}.csv"})

@app.route('/logout')
async def logout(): session.clear(); return redirect('/login')

@app.before_serving
async def startup():
    await init_db(); asyncio.create_task(cyber_bot.start())

if __name__ == "__main__":
    config = Config(); config.bind = [f"0.0.0.0:{os.getenv('PORT', '8000')}"]; asyncio.run(serve(app, config))
