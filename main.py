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
logger = logging.getLogger("Spectre.Core")

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

# --- UI TEMPLATE (SPECTRE THEME) ---
HTML_BASE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SPECTRE // ANALYTICS</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        :root {
            --bg: #050505;
            --panel: rgba(20, 20, 20, 0.95);
            --neon-green: #0f0;
            --neon-red: #f00;
            --neon-purple: #bd00ff;
            --neon-blue: #00f3ff;
            --glass: blur(10px);
            --border: 1px solid rgba(189, 0, 255, 0.3);
        }
        * { box-sizing: border-box; font-family: 'Courier New', monospace; }
        body { background: var(--bg); color: #e0e0e0; margin: 0; min-height: 100vh; display: flex; flex-direction: column; overflow-x: hidden; }
        
        #matrix-canvas { position: fixed; top: 0; left: 0; width: 100%; height: 100%; z-index: 0; opacity: 0.15; pointer-events: none; }
        
        .container { max-width: 900px; margin: 20px auto; width: 95%; position: relative; z-index: 10; padding-bottom: 50px;}
        
        .nav {
            display: flex; justify-content: space-between; align-items: center;
            background: rgba(0,0,0,0.8); backdrop-filter: var(--glass);
            padding: 15px; border-bottom: 2px solid var(--neon-purple);
            position: sticky; top: 0; z-index: 100;
            box-shadow: 0 0 20px rgba(189, 0, 255, 0.15);
        }
        .nav a { color: var(--neon-purple); text-decoration: none; font-weight: bold; font-size: 1.1rem; margin-left: 15px; letter-spacing: 1px;}
        
        .card { 
            background: var(--panel); backdrop-filter: var(--glass);
            border: var(--border); border-radius: 12px;
            padding: 20px; margin-top: 20px; 
            box-shadow: 0 10px 30px rgba(0,0,0,0.5);
            transition: transform 0.2s, border-color 0.2s;
        }
        .card:hover { border-color: var(--neon-purple); transform: translateY(-2px); }
        
        h2 { color: var(--neon-purple); margin-top: 0; text-shadow: 0 0 5px rgba(189, 0, 255, 0.3); border-bottom: 1px solid #333; padding-bottom: 10px;}
        
        .status-badge { padding: 4px 8px; border-radius: 4px; font-weight: bold; font-size: 0.8rem; }
        .online { background: rgba(0, 255, 0, 0.1); color: var(--neon-green); border: 1px solid var(--neon-green); box-shadow: 0 0 8px var(--neon-green); }
        .offline { background: rgba(255, 0, 0, 0.1); color: var(--neon-red); border: 1px solid var(--neon-red); }
        
        input, button, select { 
            width: 100%; padding: 12px; margin-top: 10px; 
            background: #050505; border: 1px solid #333; color: #fff; 
            border-radius: 6px; outline: none; transition: 0.3s; 
        }
        input:focus { border-color: var(--neon-purple); }
        button { cursor: pointer; font-weight: bold; text-transform: uppercase; background: rgba(189, 0, 255, 0.15); border: 1px solid var(--neon-purple); color: var(--neon-purple); }
        button:hover { background: var(--neon-purple); color: #fff; box-shadow: 0 0 15px var(--neon-purple); }

        .btn-small { width: auto; padding: 5px 15px; font-size: 0.8rem; margin: 0; display: inline-block; }
        .grid-item { display: flex; align-items: center; justify-content: space-between; padding: 15px 0; border-bottom: 1px solid #222; }
        
        .chart-container { position: relative; height: 250px; width: 100%; }
        table { width: 100%; border-collapse: collapse; margin-top:10px; }
        th { text-align: left; color: #888; border-bottom: 1px solid var(--neon-purple); padding: 8px; font-size: 0.8rem;}
        td { padding: 10px 8px; border-bottom: 1px solid #333; font-size: 0.9rem; }
        
        .limit-bar { height: 4px; background: #333; margin-top: 5px; border-radius: 2px; overflow: hidden; }
        .limit-fill { height: 100%; background: var(--neon-purple); }
    </style>
    <script>
        function initMatrix() {
            const canvas = document.getElementById('matrix-canvas');
            const ctx = canvas.getContext('2d');
            canvas.width = window.innerWidth;
            canvas.height = window.innerHeight;
            const alphabet = '01';
            const fontSize = 14;
            const columns = canvas.width/fontSize;
            const drops = Array(Math.floor(columns)).fill(1);
            function draw() {
                ctx.fillStyle = 'rgba(0, 0, 0, 0.05)';
                ctx.fillRect(0, 0, canvas.width, canvas.height);
                ctx.fillStyle = '#bd00ff';
                ctx.font = fontSize + 'px monospace';
                for( let i = 0; i < drops.length; i++ ) {
                    const text = alphabet.charAt(Math.floor(Math.random() * alphabet.length));
                    ctx.fillText(text, i*fontSize, drops[i]*fontSize);
                    if( drops[i]*fontSize > canvas.height && Math.random() > 0.975 ) drops[i] = 0;
                    drops[i]++;
                }
            }
            setInterval(draw, 50);
        }
        document.addEventListener("DOMContentLoaded", initMatrix);
    </script>
</head>
<body>
    <canvas id="matrix-canvas"></canvas>
    <div class="nav">
        <div><a href="/dashboard"><i class="fas fa-ghost"></i> SPECTRE</a></div>
        <div>
            {% if session.get('is_admin') %}
            <a href="/users"><i class="fas fa-users"></i> USERS</a>
            {% endif %}
            <a href="/logs"><i class="fas fa-list"></i> LOGS</a>
            <a href="/logout" style="color:var(--neon-red);">EXIT</a>
        </div>
    </div>
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
        
        # --- ROBUST SCHEMA REPAIR (Fixes 500 Error) ---
        # We explicitly check for missing columns and add them one by one.
        async def add_col(table, col, dtype):
            try:
                # Try to select the column to see if it exists
                await db.execute(f"SELECT {col} FROM {table} LIMIT 1")
            except:
                # If it fails, add the column
                logger.info(f"Repairing DB: Adding {col} to {table}")
                try:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {dtype}")
                except Exception as e:
                    logger.error(f"Failed to add {col}: {e}")

        # Fix Targets Table
        await add_col('targets', 'owner_id', 'INTEGER DEFAULT 1')
        await add_col('targets', 'tg_username', 'TEXT')
        await add_col('targets', 'phone', 'TEXT')
        await add_col('targets', 'pic_path', 'TEXT')

        # Fix Users Table
        await add_col('users', 'max_targets', 'INTEGER DEFAULT 3')
        await add_col('users', 'expiry_date', 'DATETIME')

        await db.commit()
        
        # Create Admin if missing
        async with db.execute("SELECT * FROM users WHERE username = ?", (ADMIN_USER,)) as c:
            if not await c.fetchone():
                await db.execute("INSERT INTO users (username, password, is_admin, max_targets) VALUES (?, ?, 1, 9999)", (ADMIN_USER, ADMIN_PASS))
                await db.commit()

    # Load API creds
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
    return dt_obj.strftime('%d %b %I:%M %p')

def calc_duration(start, end):
    if isinstance(start, str): start = datetime.fromisoformat(start)
    if isinstance(end, str): end = datetime.fromisoformat(end)
    diff = end - start
    total_seconds = int(diff.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0: return f"{hours}h {minutes}m"
    return f"{minutes}m {seconds}s"

async def get_heatmap_data(target_id):
    hourly = [0] * 24
    async with await get_db() as db:
        async with db.execute("SELECT start_time, end_time FROM sessions WHERE target_id=? ORDER BY id DESC LIMIT 100", (target_id,)) as c:
            rows = await c.fetchall()
    for row in rows:
        try:
            s = datetime.fromisoformat(row['start_time']) if isinstance(row['start_time'], str) else row['start_time']
            e = datetime.fromisoformat(row['end_time']) if row['end_time'] else now_tz()
            hourly[s.hour] += 1
            while s.hour != e.hour:
                s += timedelta(hours=1)
                hourly[s.hour] += 1
        except: pass
    return [min(x, 10) for x in hourly]

# --- TRACKER CORE ---
class CyberTracker:
    def __init__(self):
        self.client = None
        self.tracking_active = False

    async def start(self):
        session_str = await get_setting('session_string')
        if not runtime_api_id or not runtime_api_hash or not session_str:
            logger.warning("Config Missing. Please connect via Web UI.")
            return

        try:
            self.client = TelegramClient(StringSession(session_str), runtime_api_id, runtime_api_hash)
            await self.client.connect()
            if not await self.client.is_user_authorized():
                logger.error("Session Invalid.")
                return

            logger.info("SPECTRE ONLINE.")
            self.tracking_active = True
            asyncio.create_task(self.loop())
        except Exception as e:
            logger.error(f"Init Error: {e}")

    async def loop(self):
        while self.tracking_active:
            try:
                async with await get_db() as db:
                    async with db.execute("SELECT * FROM targets WHERE is_tracking = 1") as c:
                        targets = await c.fetchall()

                    for t in targets:
                        await self.probe_target(db, t)
                        await asyncio.sleep(0.5) 
            except Exception as e:
                logger.error(f"Loop Exception: {e}")
            await asyncio.sleep(4)

    async def probe_target(self, db, target):
        t_id = target['id']
        
        # --- PRIORITY LOGIC ---
        tg_val = None
        if target['tg_id'] and target['tg_id'] != 0:
            tg_val = target['tg_id']
        elif target['phone']:
            tg_val = target['phone']
        elif target['tg_username']:
            tg_val = target['tg_username']

        last_status = target['last_status']
        
        try:
            if not tg_val: return
            entity = await self.client.get_entity(tg_val)
            
            # Auto-resolve numeric ID if missing
            if (not target['tg_id'] or target['tg_id'] == 0) and entity.id:
                await db.execute("UPDATE targets SET tg_id=? WHERE id=?", (entity.id, t_id))

            status_obj = entity.status
            curr_status = 'offline'
            if isinstance(status_obj, UserStatusOnline): curr_status = 'online'
            elif isinstance(status_obj, UserStatusRecently): curr_status = 'recently'

            if curr_status != last_status:
                now = now_tz()
                await db.execute("UPDATE targets SET last_status=?, last_seen=? WHERE id=?", (curr_status, now, t_id))
                await db.execute("INSERT INTO logs (target_id, event_type, timestamp) VALUES (?, ?, ?)", (t_id, curr_status.upper(), now))

                if curr_status == 'online':
                    await db.execute("INSERT INTO sessions (target_id, status, start_time) VALUES (?, 'ONLINE', ?)", (t_id, now))
                elif last_status == 'online':
                    async with db.execute("SELECT id, start_time FROM sessions WHERE target_id=? AND end_time IS NULL ORDER BY id DESC LIMIT 1", (t_id,)) as c:
                        open_sess = await c.fetchone()
                    if open_sess:
                        duration = calc_duration(open_sess['start_time'], now)
                        await db.execute("UPDATE sessions SET end_time=?, duration=?, status='FINISHED' WHERE id=?", (now, duration, open_sess['id']))

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
                    if user['expiry_date']:
                        exp = datetime.fromisoformat(user['expiry_date'])
                        if datetime.now() > exp: msg = "ACCOUNT EXPIRED"
                        else:
                            session['user_id'] = user['id']
                            session['is_admin'] = bool(user['is_admin'])
                            return redirect('/dashboard')
                    else:
                        session['user_id'] = user['id']
                        session['is_admin'] = bool(user['is_admin'])
                        return redirect('/dashboard')
                if not msg: msg = "INVALID CREDENTIALS"
    
    content = f"""
    <div class="card" style="max-width:400px; margin: 100px auto; text-align:center;">
        <h2>AUTHENTICATION</h2>
        <div style="color:var(--neon-red);">{msg}</div>
        <form method="POST">
            <input name="username" placeholder="IDENTITY" required>
            <input type="password" name="password" placeholder="PASSPHRASE" required>
            <button type="submit">CONNECT</button>
        </form>
        <div style="margin-top:20px; font-size:0.8rem;">
            <a href="/register" style="color:var(--neon-purple);">CREATE ACCOUNT</a>
        </div>
    </div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/register', methods=['GET', 'POST'])
async def register():
    msg = ""
    if request.method == 'POST':
        f = await request.form
        username = f.get('username')
        password = f.get('password')
        expiry = datetime.now() + timedelta(days=14)
        max_targets = 3
        try:
            async with await get_db() as db:
                await db.execute("INSERT INTO users (username, password, is_admin, max_targets, expiry_date) VALUES (?, ?, 0, ?, ?)", 
                                 (username, password, max_targets, expiry))
                await db.commit()
            return redirect('/login')
        except:
            msg = "Username already taken."
            
    content = f"""
    <div class="card" style="max-width:400px; margin: 100px auto; text-align:center;">
        <h2>NEW AGENT ID</h2>
        <div style="color:var(--neon-red);">{msg}</div>
        <form method="POST">
            <input name="username" placeholder="Desired Username" required>
            <input type="password" name="password" placeholder="Password" required>
            <div style="text-align:left; font-size:0.7rem; color:#888; margin-top:10px;">
                * STANDARD QUOTA APPLIES
            </div>
            <button type="submit">INITIALIZE</button>
        </form>
        <div style="margin-top:20px;">
            <a href="/login" style="color:#666;">Return to Login</a>
        </div>
    </div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/dashboard')
@login_required
async def dashboard():
    is_connected = cyber_bot.tracking_active
    uid = session['user_id']
    is_admin = session.get('is_admin')

    async with await get_db() as db:
        async with db.execute("SELECT * FROM users WHERE id=?", (uid,)) as c: user_data = await c.fetchone()
        
        if is_admin:
            query = "SELECT t.*, u.username as owner FROM targets t LEFT JOIN users u ON t.owner_id = u.id ORDER BY t.last_status DESC"
            args = ()
        else:
            query = "SELECT t.*, 'ME' as owner FROM targets t WHERE owner_id=? ORDER BY t.last_status DESC"
            args = (uid,)
        async with db.execute(query, args) as c: targets = await c.fetchall()

    current_count = len([t for t in targets if t.get('owner') == 'ME' or not is_admin])
    max_targets = user_data['max_targets']
    limit_pct = min(100, int((current_count / max_targets) * 100))
    limit_color = "var(--neon-blue)" if limit_pct < 80 else "var(--neon-red)"
    
    expiry_txt = "LIFETIME"
    if user_data['expiry_date']:
        exp_d = datetime.fromisoformat(str(user_data['expiry_date']))
        days = (exp_d - datetime.now()).days
        expiry_txt = f"{days} DAYS LEFT"

    rows = ""
    for t in targets:
        cls = "online" if t['last_status'] == 'online' else "offline"
        ts = fmt_time(t['last_seen'])
        ident = t['tg_id']
        if not ident or ident == 0: ident = t['phone']
        if not ident: ident = t['tg_username']
        
        owner_badge = f"<span style='font-size:0.6rem; color:#666; border:1px solid #333; padding:2px 4px; border-radius:3px;'>{t['owner']}</span>" if is_admin else ""
        rows += f"""<div class="card grid-item" onclick="location.href='/target/{t['id']}'" style="cursor:pointer;"><div><div style="font-size:1.1rem; font-weight:bold; color:#fff;">{t['display_name']} {owner_badge}</div><div style="font-size:0.8rem; color:var(--neon-purple);">{ident}</div></div><div style="text-align:right;"><span class="status-badge {cls}">{t['last_status'].upper()}</span><div style="font-size:0.7rem; color:#666; margin-top:5px;">{ts}</div></div></div>"""

    status_alert = ""
    if not is_connected: status_alert = """<div style="background:rgba(255,0,0,0.2); padding:10px; border:1px solid red; margin-bottom:10px; text-align:center;">⚠️ UPLINK OFFLINE <a href="/connect" style="color:#fff; text-decoration:underline;">CONNECT TELEGRAM</a></div>"""

    content = f"""
    {status_alert}
    <div style="margin-bottom:20px;">
        <div style="display:flex; justify-content:space-between; font-size:0.8rem; color:#888;">
            <span>QUOTA: {current_count}/{max_targets}</span>
            <span>ACCESS: {expiry_txt}</span>
        </div>
        <div class="limit-bar"><div class="limit-fill" style="width:{limit_pct}%; background:{limit_color};"></div></div>
    </div>
    <div style="display:flex; justify-content:space-between; align-items:center;">
        <h2 style="margin:0;">TARGET GRID</h2>
        <a href="/add"><button class="btn-small" { 'disabled style="opacity:0.5"' if current_count >= max_targets else ''}>+ NEW TARGET</button></a>
    </div>
    {rows}
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/add', methods=['GET', 'POST'])
@login_required
async def add():
    uid = session['user_id']
    async with await get_db() as db:
        async with db.execute("SELECT max_targets FROM users WHERE id=?", (uid,)) as c: user_info = await c.fetchone()
        async with db.execute("SELECT COUNT(*) as c FROM targets WHERE owner_id=?", (uid,)) as c: current_count = (await c.fetchone())['c']
    if current_count >= user_info['max_targets']: return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', '<div class="card"><h2>QUOTA EXCEEDED</h2><p>Limit reached.</p><a href="/dashboard">Back</a></div>'))

    if request.method == 'POST':
        f = await request.form
        uid_input = f.get('user_id')
        phone_input = f.get('phone')
        username_input = f.get('username')
        tg_id = int(uid_input) if uid_input and uid_input.isdigit() else 0
        
        try:
            async with await get_db() as db:
                await db.execute("INSERT INTO targets (owner_id, tg_id, tg_username, phone, display_name, last_status) VALUES (?,?,?,?, ?, 'unknown')", 
                                (uid, tg_id, username_input, phone_input, f.get('name')))
                await db.commit()
            return redirect('/dashboard')
        except Exception as e:
            logger.error(f"ADD TARGET ERROR: {e}")
            return f"INTERNAL ERROR: {e} - PLEASE REPORT TO ADMIN"
    
    content = """
    <div class="card">
        <h2>ADD TARGET</h2>
        <form method="POST">
            <label>Display Name (Required)</label>
            <input name="name" required placeholder="Target Alias">
            <label style="color:var(--neon-green)">PRIORITY 1: User ID (Numeric)</label>
            <input name="user_id" placeholder="123456789">
            <label style="color:var(--neon-green)">PRIORITY 1: Phone Number</label>
            <input name="phone" placeholder="+91...">
            <label style="color:var(--neon-blue)">PRIORITY 2: Telegram Username</label>
            <input name="username" placeholder="@username">
            <button type="submit">INITIATE TRACKING</button>
        </form>
    </div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/users', methods=['GET', 'POST'])
@login_required
@admin_required
async def manage_users():
    msg = ""
    if request.method == 'POST':
        f = await request.form
        if 'delete_id' in f:
            uid = f.get('delete_id')
            if int(uid) == session['user_id']: msg = "Cannot delete self."
            else:
                async with await get_db() as db:
                    await db.execute("DELETE FROM users WHERE id=?", (uid,))
                    await db.commit()
        else:
            user = f.get('username')
            pw = f.get('password')
            is_admin = 1 if f.get('role') == 'admin' else 0
            max_t = int(f.get('max_targets'))
            days = int(f.get('validity'))
            expiry = None
            if days > 0: expiry = datetime.now() + timedelta(days=days)
            try:
                async with await get_db() as db:
                    await db.execute("INSERT INTO users (username, password, is_admin, max_targets, expiry_date) VALUES (?, ?, ?, ?, ?)", (user, pw, is_admin, max_t, expiry))
                    await db.commit()
                msg = "User Added."
            except: msg = "Username taken."
    async with await get_db() as db:
        async with db.execute("SELECT * FROM users") as c: users = await c.fetchall()

    user_rows = ""
    for u in users:
        role = "ADMIN" if u['is_admin'] else "VIEWER"
        exp = "NEVER"
        if u['expiry_date']:
            exp_date = datetime.fromisoformat(str(u['expiry_date']))
            days_left = (exp_date - datetime.now()).days
            exp = f"{days_left} DAYS" if days_left > 0 else "EXPIRED"
        del_btn = ""
        if u['id'] != session['user_id']: del_btn = f"<form method='POST' style='display:inline;'><input type='hidden' name='delete_id' value='{u['id']}'><button class='btn-small' style='background:var(--neon-red); padding:5px 10px;'>DEL</button></form>"
        user_rows += f"<tr><td>{u['username']}</td><td>{role}</td><td>{u['max_targets']}</td><td>{exp}</td><td style='text-align:right;'>{del_btn}</td></tr>"

    content = f"""
    <div class="card"><h2>ACCESS CONTROL</h2><div style="color:var(--neon-blue); margin-bottom:10px;">{msg}</div>
    <form method="POST" style="display:grid; grid-template-columns: 1fr 1fr; gap:10px; margin-bottom:20px;">
        <div><label>Username</label><input name="username" required></div><div><label>Password</label><input name="password" required></div>
        <div><label>Role</label><select name="role"><option value="viewer">User</option><option value="admin">Admin</option></select></div>
        <div><label>Max Targets</label><input type="number" name="max_targets" value="5" required></div>
        <div><label>Validity (Days, 0=Forever)</label><input type="number" name="validity" value="30" required></div>
        <div style="display:flex; align-items:flex-end;"><button class="btn-small" style="height:45px; width:100%;">PROVISION</button></div>
    </form>
    <table><tr><th>USER</th><th>ROLE</th><th>LIMIT</th><th>VALIDITY</th><th>ACT</th></tr>{user_rows}</table></div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/logs')
@login_required
async def view_logs():
    async with await get_db() as db:
        query = "SELECT l.timestamp, l.event_type, t.display_name FROM logs l LEFT JOIN targets t ON l.target_id = t.id ORDER BY l.timestamp DESC LIMIT 100"
        async with db.execute(query) as c: logs = await c.fetchall()
    log_rows = "".join([f"<tr><td style='color:#666;'>{fmt_time(l['timestamp'])}</td><td style='color:#fff;'>{l['display_name'] or 'SYSTEM'}</td><td style='color:{'var(--neon-green)' if l['event_type']=='ONLINE' else 'var(--neon-red)'};'>{l['event_type']}</td></tr>" for l in logs])
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', f"<div class='card'><h2>SYSTEM LOGS</h2><table><tr><th>TIME</th><th>ENTITY</th><th>EVENT</th></tr>{log_rows}</table></div>"))

@app.route('/target/<int:t_id>')
@login_required
async def target_detail(t_id):
    async with await get_db() as db:
        async with db.execute("SELECT * FROM targets WHERE id=?", (t_id,)) as c: target = await c.fetchone()
        async with db.execute("SELECT * FROM sessions WHERE target_id=? ORDER BY id DESC LIMIT 20", (t_id,)) as c: sessions = await c.fetchall()
    if not target: return "Not Found"
    if not session.get('is_admin') and target['owner_id'] != session['user_id']: return "Access Denied"
    heatmap_data = await get_heatmap_data(t_id)
    session_rows = "".join([f"<tr><td>{fmt_time(s['start_time'])}</td><td>{fmt_time(s['end_time'])}</td><td>{s['duration'] or 'Active'}</td></tr>" for s in sessions])
    content = f"""<div style="margin-bottom:15px;"><a href="/dashboard" style="color:#888;">&larr; BACK</a></div><div class="card" style="text-align:center;"><h1 style="color:#fff; margin-bottom:5px;">{target['display_name']}</h1><div style="color:var(--neon-purple); margin-bottom:20px;">{target['tg_username'] or target['tg_id']}</div><div style="display:flex; justify-content:center; gap:20px; margin-bottom:20px;"><div><div style="font-size:0.8rem; color:#888;">STATUS</div><div style="font-size:1.2rem;" class="{ 'online' if target['last_status']=='online' else 'offline' }">{target['last_status'].upper()}</div></div><div><div style="font-size:0.8rem; color:#888;">LAST SEEN</div><div style="font-size:1.2rem; color:#fff;">{fmt_time(target['last_seen'])}</div></div></div><a href="/export/{t_id}"><button class="btn-small" style="background:#222;">DOWNLOAD CSV</button></a><a href="/delete/{t_id}" onclick="return confirm('Delete?');"><button class="btn-small" style="background:var(--neon-red); color:#fff; border:none;">DELETE</button></a></div><div class="card"><h2>ACTIVITY HEATMAP</h2><div class="chart-container"><canvas id="activityChart"></canvas></div></div><div class="card"><h2>SESSIONS</h2><table><tr><th>ONLINE</th><th>OFFLINE</th><th>DURATION</th></tr>{session_rows}</table></div><script>new Chart(document.getElementById('activityChart'), {{type: 'bar',data: {{ labels: Array.from({{length:24}},(_,i)=>i+":00"), datasets: [{{ label: 'Activity', data: {heatmap_data}, backgroundColor: 'rgba(189, 0, 255, 0.5)', borderRadius: 4 }}] }},options: {{ responsive: true, maintainAspectRatio: false, scales: {{ y: {{ beginAtZero: true, grid: {{ color: '#333' }} }}, x: {{ grid: {{ display: false }} }} }} }}}});</script>"""
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/delete/<int:t_id>')
@login_required
async def delete_target(t_id):
    async with await get_db() as db:
        async with db.execute("SELECT owner_id FROM targets WHERE id=?", (t_id,)) as c: row = await c.fetchone()
        if not row: return "Not Found"
        if not session.get('is_admin') and row['owner_id'] != session['user_id']: return "Denied"
        await db.execute("DELETE FROM targets WHERE id=?", (t_id,)); await db.commit()
    return redirect('/dashboard')

@app.route('/export/<int:t_id>')
@login_required
async def export_csv(t_id):
    async with await get_db() as db:
        async with db.execute("SELECT * FROM sessions WHERE target_id=? ORDER BY id DESC", (t_id,)) as c: rows = await c.fetchall()
    si = io.StringIO(); cw = csv.writer(si)
    cw.writerow(['Status', 'Start Time', 'End Time', 'Duration'])
    for r in rows: cw.writerow([r['status'], r['start_time'], r['end_time'], r['duration']])
    return Response(si.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=log_{t_id}.csv"})

@app.route('/api/status')
async def api_status():
    async with await get_db() as db:
        async with db.execute("SELECT COUNT(*) as c FROM targets WHERE last_status = 'online'") as c: online = (await c.fetchone())['c']
    return jsonify({"system": "SPECTRE_V1", "status": "OPERATIONAL", "online_targets": online})

@app.route('/connect', methods=['GET', 'POST'])
@login_required
async def connect():
    global temp_client, phone_number, phone_code_hash, runtime_api_id, runtime_api_hash
    msg = ""; val_aid = runtime_api_id if runtime_api_id else ""; val_hash = runtime_api_hash if runtime_api_hash else ""
    if request.method == 'POST':
        f = await request.form; phone = f.get('phone'); aid = f.get('api_id'); ahash = f.get('api_hash')
        if aid and ahash: runtime_api_id = int(aid); runtime_api_hash = ahash; await save_setting('api_id', aid); await save_setting('api_hash', ahash)
        try:
            temp_client = TelegramClient(StringSession(), runtime_api_id, runtime_api_hash); await temp_client.connect(); send = await temp_client.send_code_request(phone); phone_number = phone; phone_code_hash = send.phone_code_hash; return redirect('/verify')
        except Exception as e: msg = f"Error: {e}"
    content = f"""<div class="card"><h2>LINK UPLINK</h2><div style="color:red">{msg}</div><form method="POST"><label>API ID</label><input name="api_id" value="{val_aid}" required><label>API HASH</label><input name="api_hash" value="{val_hash}" required><label>Phone</label><input name="phone" placeholder="+91..." required><button>SEND OTP</button></form></div>"""
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/verify', methods=['GET', 'POST'])
@login_required
async def verify():
    global temp_client; msg = ""
    if request.method == 'POST':
        code = (await request.form).get('code')
        try:
            await temp_client.sign_in(phone=phone_number, code=code, phone_code_hash=phone_code_hash); await save_setting('session_string', temp_client.session.save()); await temp_client.disconnect(); asyncio.create_task(cyber_bot.start()); return redirect('/dashboard')
        except errors.SessionPasswordNeededError: return redirect('/2fa')
        except Exception as e: msg = f"Error: {e}"
    content = f"""<div class="card"><h2>ENTER OTP</h2><div style="color:red">{msg}</div><form method="POST"><input name="code" placeholder="12345" required><button>VERIFY</button></form></div>"""
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/2fa', methods=['GET', 'POST'])
@login_required
async def two_fa():
    global temp_client
    if request.method == 'POST':
        pw = (await request.form).get('password')
        try: await temp_client.sign_in(password=pw); await save_setting('session_string', temp_client.session.save()); await temp_client.disconnect(); asyncio.create_task(cyber_bot.start()); return redirect('/dashboard')
        except Exception as e: return f"Error: {e}"
    content = """<div class="card"><h2>CLOUD PASSWORD</h2><form method="POST"><input type="password" name="password" required><button>UNLOCK</button></form></div>"""
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/logout')
async def logout(): session.clear(); return redirect('/login')

@app.before_serving
async def startup():
    await init_db(); asyncio.create_task(cyber_bot.start())

if __name__ == "__main__":
    config = Config(); config.bind = [f"0.0.0.0:{os.getenv('PORT', '8000')}"]; asyncio.run(serve(app, config))
