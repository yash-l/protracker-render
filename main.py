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
logger = logging.getLogger("Netrunner.Enterprise")

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
    max_targets INTEGER DEFAULT 5,
    expiry_date DATETIME
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER,
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

# --- UI TEMPLATE ---
HTML_BASE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NETRUNNER // ENTERPRISE</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        :root {
            --bg: #020202;
            --panel: rgba(15, 15, 15, 0.90);
            --neon-green: #0f0;
            --neon-red: #f00;
            --neon-blue: #00f3ff;
            --neon-gold: #ffaa00;
            --glass: blur(10px);
            --border: 1px solid rgba(0, 243, 255, 0.2);
        }
        * { box-sizing: border-box; font-family: 'Courier New', monospace; }
        body { background: var(--bg); color: #e0e0e0; margin: 0; min-height: 100vh; display: flex; flex-direction: column; overflow-x: hidden; }
        
        #matrix-canvas { position: fixed; top: 0; left: 0; width: 100%; height: 100%; z-index: 0; opacity: 0.15; pointer-events: none; }
        
        .container { max-width: 900px; margin: 20px auto; width: 95%; position: relative; z-index: 10; padding-bottom: 50px;}
        
        .nav {
            display: flex; justify-content: space-between; align-items: center;
            background: rgba(0,0,0,0.8); backdrop-filter: var(--glass);
            padding: 15px; border-bottom: 2px solid var(--neon-green);
            position: sticky; top: 0; z-index: 100;
            box-shadow: 0 0 20px rgba(0, 255, 0, 0.1);
        }
        .nav a { color: var(--neon-blue); text-decoration: none; font-weight: bold; font-size: 1.0rem; margin-left: 15px;}
        
        .card { 
            background: var(--panel); backdrop-filter: var(--glass);
            border: var(--border); border-radius: 12px;
            padding: 20px; margin-top: 20px; 
            box-shadow: 0 10px 30px rgba(0,0,0,0.5);
            transition: transform 0.2s, border-color 0.2s;
        }
        .card:hover { border-color: var(--neon-green); transform: translateY(-2px); }
        
        h2 { color: var(--neon-green); margin-top: 0; text-shadow: 0 0 5px rgba(0, 255, 0, 0.3); }
        
        .status-badge { padding: 4px 8px; border-radius: 4px; font-weight: bold; font-size: 0.8rem; }
        .online { background: rgba(0, 255, 0, 0.1); color: var(--neon-green); border: 1px solid var(--neon-green); box-shadow: 0 0 8px var(--neon-green); }
        .offline { background: rgba(255, 0, 0, 0.1); color: var(--neon-red); border: 1px solid var(--neon-red); }
        
        input, button, select { 
            width: 100%; padding: 12px; margin-top: 10px; 
            background: #050505; border: 1px solid #333; color: var(--neon-blue); 
            border-radius: 6px; outline: none; transition: 0.3s; 
        }
        input:focus { border-color: var(--neon-green); }
        button { cursor: pointer; font-weight: bold; text-transform: uppercase; background: rgba(0, 243, 255, 0.1); }
        button:hover { background: var(--neon-green); color: #000; box-shadow: 0 0 15px var(--neon-green); }

        .btn-small { width: auto; padding: 5px 15px; font-size: 0.8rem; margin: 0; display: inline-block; }
        .grid-item { display: flex; align-items: center; justify-content: space-between; padding: 15px 0; border-bottom: 1px solid #222; }
        
        .chart-container { position: relative; height: 250px; width: 100%; }
        table { width: 100%; border-collapse: collapse; margin-top:10px; }
        th { text-align: left; color: #888; border-bottom: 1px solid var(--neon-green); padding: 8px; font-size: 0.8rem;}
        td { padding: 10px 8px; border-bottom: 1px solid #333; font-size: 0.9rem; }
        
        .limit-bar { height: 4px; background: #333; margin-top: 5px; border-radius: 2px; overflow: hidden; }
        .limit-fill { height: 100%; background: var(--neon-blue); }
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
                ctx.fillStyle = '#0F0';
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
        <div><a href="/dashboard">NETRUNNER // CORE</a></div>
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
        
        # --- MIGRATIONS (Auto-upgrade DB) ---
        try:
            await db.execute("SELECT owner_id FROM targets LIMIT 1")
        except:
            logger.info("Migrating: Adding owner_id to targets")
            await db.execute("ALTER TABLE targets ADD COLUMN owner_id INTEGER DEFAULT 1")
        
        try:
            await db.execute("SELECT max_targets FROM users LIMIT 1")
        except:
            logger.info("Migrating: Adding limits to users")
            await db.execute("ALTER TABLE users ADD COLUMN max_targets INTEGER DEFAULT 5")
            await db.execute("ALTER TABLE users ADD COLUMN expiry_date DATETIME")

        await db.commit()
        
        # Create Admin
        async with db.execute("SELECT * FROM users WHERE username = ?", (ADMIN_USER,)) as c:
            if not await c.fetchone():
                # Admin gets 9999 targets and no expiry
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

            logger.info("NETRUNNER ONLINE.")
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
        tg_id = target['tg_id'] if target['tg_id'] else target['tg_username']
        last_status = target['last_status']
        
        try:
            if not tg_id: return
            entity = await self.client.get_entity(tg_id)
            
            if isinstance(tg_id, str) and entity.id:
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

# --- MIDDLEWARE & ROUTES ---
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
                    # CHECK VALIDITY
                    if user['expiry_date']:
                        exp = datetime.fromisoformat(user['expiry_date'])
                        if datetime.now() > exp:
                            msg = "ACCOUNT EXPIRED"
                            user = None # Deny login
                    
                    if user:
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
        # User Stats
        async with db.execute("SELECT * FROM users WHERE id=?", (uid,)) as c:
            user_data = await c.fetchone()
        
        # Targets (Admin sees all, User sees owned)
        if is_admin:
            query = "SELECT t.*, u.username as owner FROM targets t LEFT JOIN users u ON t.owner_id = u.id ORDER BY t.last_status DESC"
            args = ()
        else:
            query = "SELECT t.*, 'ME' as owner FROM targets t WHERE owner_id=? ORDER BY t.last_status DESC"
            args = (uid,)
            
        async with db.execute(query, args) as c:
            targets = await c.fetchall()

    # Limit Logic
    current_count = len([t for t in targets if t.get('owner') == 'ME' or not is_admin])
    max_targets = user_data['max_targets']
    limit_pct = min(100, int((current_count / max_targets) * 100))
    limit_color = "var(--neon-blue)" if limit_pct < 80 else "var(--neon-red)"

    rows = ""
    for t in targets:
        cls = "online" if t['last_status'] == 'online' else "offline"
        ts = fmt_time(t['last_seen'])
        ident = t['tg_username'] if t['tg_username'] else t['tg_id']
        owner_badge = f"<span style='font-size:0.6rem; color:#666; border:1px solid #333; padding:2px 4px; border-radius:3px;'>{t['owner']}</span>" if is_admin else ""
        
        rows += f"""
        <div class="card grid-item" onclick="location.href='/target/{t['id']}'" style="cursor:pointer;">
            <div>
                <div style="font-size:1.1rem; font-weight:bold; color:#fff;">{t['display_name']} {owner_badge}</div>
                <div style="font-size:0.8rem; color:var(--neon-blue);">{ident}</div>
            </div>
            <div style="text-align:right;">
                <span class="status-badge {cls}">{t['last_status'].upper()}</span>
                <div style="font-size:0.7rem; color:#666; margin-top:5px;">{ts}</div>
            </div>
        </div>
        """

    status_alert = ""
    if not is_connected:
        status_alert = """<div style="background:rgba(255,0,0,0.2); padding:10px; border:1px solid red; margin-bottom:10px; text-align:center;">
        ⚠️ UPLINK OFFLINE <a href="/connect" style="color:#fff; text-decoration:underline;">CONNECT TELEGRAM</a>
        </div>"""

    content = f"""
    {status_alert}
    <div style="margin-bottom:20px;">
        <div style="display:flex; justify-content:space-between; font-size:0.8rem; color:#888;">
            <span>RESOURCE QUOTA</span>
            <span>{current_count} / {max_targets} TARGETS</span>
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

# --- USER MANAGEMENT ---
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
            if days > 0:
                expiry = datetime.now() + timedelta(days=days)

            try:
                async with await get_db() as db:
                    await db.execute("INSERT INTO users (username, password, is_admin, max_targets, expiry_date) VALUES (?, ?, ?, ?, ?)", 
                                     (user, pw, is_admin, max_t, expiry))
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

        delete_btn = ""
        if u['id'] != session['user_id']:
            delete_btn = f"""<form method="POST" style="display:inline;"><input type="hidden" name="delete_id" value="{u['id']}"><button class="btn-small" style="background:var(--neon-red); padding:5px 10px;">DEL</button></form>"""
        
        user_rows += f"""<tr><td>{u['username']}</td><td>{role}</td><td>{u['max_targets']}</td><td>{exp}</td><td style="text-align:right;">{delete_btn}</td></tr>"""

    content = f"""
    <div class="card">
        <h2>USER ACCESS CONTROL</h2>
        <div style="color:var(--neon-blue); margin-bottom:10px;">{msg}</div>
        <form method="POST" style="display:grid; grid-template-columns: 1fr 1fr; gap:10px; margin-bottom:20px;">
            <div><label>Username</label><input name="username" required></div>
            <div><label>Password</label><input name="password" required></div>
            <div><label>Role</label><select name="role"><option value="viewer">User</option><option value="admin">Admin</option></select></div>
            <div><label>Max Targets</label><input type="number" name="max_targets" value="5" required></div>
            <div><label>Validity (Days, 0=Forever)</label><input type="number" name="validity" value="30" required></div>
            <div style="display:flex; align-items:flex-end;"><button class="btn-small" style="height:45px; width:100%;">PROVISION USER</button></div>
        </form>
        <table><tr><th>USER</th><th>ROLE</th><th>LIMIT</th><th>VALIDITY</th><th>ACT</th></tr>{user_rows}</table>
    </div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

# --- ADD TARGET (WITH LIMIT CHECK) ---
@app.route('/add', methods=['GET', 'POST'])
@login_required
async def add():
    uid = session['user_id']
    
    # CHECK LIMITS
    async with await get_db() as db:
        async with db.execute("SELECT max_targets FROM users WHERE id=?", (uid,)) as c:
            user_info = await c.fetchone()
        async with db.execute("SELECT COUNT(*) as c FROM targets WHERE owner_id=?", (uid,)) as c:
            current_count = (await c.fetchone())['c']
    
    if current_count >= user_info['max_targets']:
        return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', 
            '<div class="card"><h2>QUOTA EXCEEDED</h2><p>You have reached your tracking limit.</p><a href="/dashboard">Back</a></div>'))

    if request.method == 'POST':
        f = await request.form
        tg_in = f.get('tg_input')
        tg_id = int(tg_in) if tg_in.isdigit() else 0
        tg_user = tg_in if not tg_in.isdigit() else None
        
        async with await get_db() as db:
            await db.execute("INSERT INTO targets (owner_id, tg_id, tg_username, display_name, last_status) VALUES (?,?,?,?, 'unknown')", 
                             (uid, tg_id, tg_user, f.get('name')))
            await db.commit()
        return redirect('/dashboard')
    
    content = """<div class="card"><h2>ADD TARGET</h2><form method="POST"><label>Name</label><input name="name" required><label>Telegram ID or Username</label><input name="tg_input" required><button type="submit">INITIATE TRACKING</button></form></div>"""
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

# --- LOGS PAGE ---
@app.route('/logs')
@login_required
async def view_logs():
    async with await get_db() as db:
        query = "SELECT l.timestamp, l.event_type, t.display_name FROM logs l LEFT JOIN targets t ON l.target_id = t.id ORDER BY l.timestamp DESC LIMIT 100"
        async with db.execute(query) as c: logs = await c.fetchall()

    log_rows = ""
    for l in logs:
        name = l['display_name'] if l['display_name'] else "SYSTEM"
        color = "var(--neon-green)" if l['event_type'] == 'ONLINE' else "var(--neon-red)"
        log_rows += f"<tr><td style='color:#666;'>{fmt_time(l['timestamp'])}</td><td style='color:#fff;'>{name}</td><td style='color:{color};'>{l['event_type']}</td></tr>"

    content = f"<div class='card'><h2>SYSTEM LOGS</h2><table><tr><th>TIME</th><th>ENTITY</th><th>EVENT</th></tr>{log_rows}</table></div>"
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

# --- TARGET DETAIL, EXPORT, DELETE ---
@app.route('/target/<int:t_id>')
@login_required
async def target_detail(t_id):
    async with await get_db() as db:
        async with db.execute("SELECT * FROM targets WHERE id=?", (t_id,)) as c: target = await c.fetchone()
        async with db.execute("SELECT * FROM sessions WHERE target_id=? ORDER BY id DESC LIMIT 20", (t_id,)) as c: sessions = await c.fetchall()
    
    if not target: return "Not Found"
    # Ownership Check
    if not session.get('is_admin') and target['owner_id'] != session['user_id']: return "Access Denied"

    heatmap_data = await get_heatmap_data(t_id)
    session_rows = "".join([f"<tr><td>{fmt_time(s['start_time'])}</td><td>{fmt_time(s['end_time'])}</td><td>{s['duration'] or 'Active'}</td></tr>" for s in sessions])

    content = f"""
    <div style="margin-bottom:15px;"><a href="/dashboard" style="color:#888;">&larr; BACK</a></div>
    <div class="card" style="text-align:center;">
        <h1 style="color:#fff; margin-bottom:5px;">{target['display_name']}</h1>
        <div style="color:var(--neon-blue); margin-bottom:20px;">{target['tg_username'] or target['tg_id']}</div>
        <div style="display:flex; justify-content:center; gap:20px; margin-bottom:20px;">
            <div><div style="font-size:0.8rem; color:#888;">STATUS</div><div style="font-size:1.2rem;" class="{ 'online' if target['last_status']=='online' else 'offline' }">{target['last_status'].upper()}</div></div>
            <div><div style="font-size:0.8rem; color:#888;">LAST SEEN</div><div style="font-size:1.2rem; color:#fff;">{fmt_time(target['last_seen'])}</div></div>
        </div>
        <a href="/export/{t_id}"><button class="btn-small" style="background:#222;">DOWNLOAD CSV</button></a>
        <a href="/delete/{t_id}" onclick="return confirm('Delete?');"><button class="btn-small" style="background:var(--neon-red); color:#fff; border:none;">DELETE</button></a>
    </div>
    <div class="card"><h2>ACTIVITY HEATMAP</h2><div class="chart-container"><canvas id="activityChart"></canvas></div></div>
    <div class="card"><h2>SESSIONS</h2><table><tr><th>ONLINE</th><th>OFFLINE</th><th>DURATION</th></tr>{session_rows}</table></div>
    <script>
        new Chart(document.getElementById('activityChart'), {{
            type: 'bar',
            data: {{ labels: Array.from({{length:24}},(_,i)=>i+":00"), datasets: [{{ label: 'Activity', data: {heatmap_data}, backgroundColor: 'rgba(0, 243, 255, 0.5)', borderRadius: 4 }}] }},
            options: {{ responsive: true, maintainAspectRatio: false, scales: {{ y: {{ beginAtZero: true, grid: {{ color: '#333' }} }}, x: {{ grid: {{ display: false }} }} }} }}
        }});
    </script>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/delete/<int:t_id>')
@login_required
async def delete_target(t_id):
    async with await get_db() as db:
        # Check ownership
        async with db.execute("SELECT owner_id FROM targets WHERE id=?", (t_id,)) as c: row = await c.fetchone()
        if not row: return "Not Found"
        if not session.get('is_admin') and row['owner_id'] != session['user_id']: return "Denied"
        
        await db.execute("DELETE FROM targets WHERE id=?", (t_id,))
        await db.commit()
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

# --- TELEGRAM CONNECT ---
@app.route('/connect', methods=['GET', 'POST'])
@login_required
async def connect():
    global temp_client, phone_number, phone_code_hash, runtime_api_id, runtime_api_hash
    msg = ""
    val_aid = runtime_api_id if runtime_api_id else ""
    val_hash = runtime_api_hash if runtime_api_hash else ""
    
    if request.method == 'POST':
        f = await request.form
        phone = f.get('phone')
        aid = f.get('api_id')
        ahash = f.get('api_hash')
        if aid and ahash:
            runtime_api_id = int(aid); runtime_api_hash = ahash
            await save_setting('api_id', aid); await save_setting('api_hash', ahash)
        try:
            temp_client = TelegramClient(StringSession(), runtime_api_id, runtime_api_hash)
            await temp_client.connect()
            send = await temp_client.send_code_request(phone)
            phone_number = phone; phone_code_hash = send.phone_code_hash
            return redirect('/verify')
        except Exception as e: msg = f"Error: {e}"

    content = f"""<div class="card"><h2>LINK UPLINK</h2><div style="color:red">{msg}</div><form method="POST"><label>API ID</label><input name="api_id" value="{val_aid}" required><label>API HASH</label><input name="api_hash" value="{val_hash}" required><label>Phone</label><input name="phone" placeholder="+91..." required><button>SEND OTP</button></form></div>"""
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/verify', methods=['GET', 'POST'])
@login_required
async def verify():
    global temp_client
    msg = ""
    if request.method == 'POST':
        code = (await request.form).get('code')
        try:
            await temp_client.sign_in(phone=phone_number, code=code, phone_code_hash=phone_code_hash)
            await save_setting('session_string', temp_client.session.save())
            await temp_client.disconnect()
            asyncio.create_task(cyber_bot.start())
            return redirect('/dashboard')
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
        try:
            await temp_client.sign_in(password=pw)
            await save_setting('session_string', temp_client.session.save())
            await temp_client.disconnect()
            asyncio.create_task(cyber_bot.start())
            return redirect('/dashboard')
        except Exception as e: return f"Error: {e}"
    content = """<div class="card"><h2>CLOUD PASSWORD</h2><form method="POST"><input type="password" name="password" required><button>UNLOCK</button></form></div>"""
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content)
)

@app.route('/logout')
async def logout(): session.clear(); return redirect('/login')

@app.before_serving
async def startup():
    await init_db()
    asyncio.create_task(cyber_bot.start())

if __name__ == "__main__":
    config = Config()
    config.bind = [f"0.0.0.0:{os.getenv('PORT', '8000')}"]
    asyncio.run(serve(app, config))
