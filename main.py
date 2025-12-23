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

# --- CONFIGURATION DEFAULTS ---
# We use global variables that can be updated via the Web UI
DEFAULT_API_ID = os.getenv("API_ID", "")
DEFAULT_API_HASH = os.getenv("API_HASH", "")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
TIMEZONE = 'Asia/Kolkata'
TZ = pytz.timezone(TIMEZONE)

# --- LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(name)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("Netrunner.Pro")

# --- APP SETUP ---
app = Quart(__name__)
app.secret_key = SECRET_KEY

# Global vars for Login Flow
temp_client = None
phone_number = None
phone_code_hash = None
# These will store the credentials provided via Web UI
runtime_api_id = int(DEFAULT_API_ID) if DEFAULT_API_ID.isdigit() else 0
runtime_api_hash = DEFAULT_API_HASH

# --- DATABASE SCHEMA ---
DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE,
    password TEXT,
    is_admin BOOLEAN DEFAULT 0,
    status TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    <title>NETRUNNER // ANALYTICS</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        :root {
            --bg: #020202;
            --panel: rgba(15, 15, 15, 0.85);
            --neon-green: #0f0;
            --neon-red: #f00;
            --neon-blue: #00f3ff;
            --glass: blur(10px);
            --border: 1px solid rgba(0, 243, 255, 0.2);
        }
        * { box-sizing: border-box; font-family: 'Courier New', monospace; }
        body { background: var(--bg); color: #e0e0e0; margin: 0; min-height: 100vh; display: flex; flex-direction: column; overflow-x: hidden; }
        
        #matrix-canvas { position: fixed; top: 0; left: 0; width: 100%; height: 100%; z-index: 0; opacity: 0.15; pointer-events: none; }
        
        .container { max-width: 800px; margin: 20px auto; width: 95%; position: relative; z-index: 10; padding-bottom: 50px;}
        
        .nav {
            display: flex; justify-content: space-between; align-items: center;
            background: rgba(0,0,0,0.8); backdrop-filter: var(--glass);
            padding: 15px; border-bottom: 2px solid var(--neon-green);
            position: sticky; top: 0; z-index: 100;
            box-shadow: 0 0 20px rgba(0, 255, 0, 0.1);
        }
        .nav a { color: var(--neon-blue); text-decoration: none; font-weight: bold; font-size: 1.1rem; }
        
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
        
        input, button { 
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
        
        label { font-size: 0.8rem; color: #888; display: block; margin-top: 10px; }
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
        <a href="/dashboard">NETRUNNER // PRO</a>
        <div>
            <a href="/logs" style="font-size:0.9rem; margin-right:15px;">LOGS</a>
            <a href="/logout" style="color:var(--neon-red); font-size:0.9rem;">EXIT</a>
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
        async with db.execute("SELECT * FROM users WHERE username = ?", (ADMIN_USER,)) as c:
            if not await c.fetchone():
                await db.execute("INSERT INTO users (username, password, is_admin) VALUES (?, ?, 1)", (ADMIN_USER, ADMIN_PASS))
                await db.commit()
                print(f"\n[ADMIN] {ADMIN_USER} / {ADMIN_PASS}\n")
    
    # Load saved API credentials if they exist
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

# --- HELPER FUNCTIONS ---
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

# --- TRACKER LOGIC ---
class CyberTracker:
    def __init__(self):
        self.client = None
        self.tracking_active = False

    async def start(self):
        session_str = await get_setting('session_string')
        # Ensure we have API creds loaded
        if not runtime_api_id or not runtime_api_hash:
            logger.warning("No API Credentials found. Please connect via Web UI.")
            return

        if not session_str:
            logger.warning("No Session String found. Please connect via Web UI.")
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

# --- WEB ROUTES ---
def login_required(f):
    @wraps(f)
    async def decorated(*args, **kwargs):
        if 'user_id' not in session: return redirect('/login')
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
                    session['user_id'] = user['id']
                    return redirect('/dashboard')
                msg = "ACCESS DENIED"
    
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
    async with await get_db() as db:
        async with db.execute("SELECT * FROM targets ORDER BY last_status DESC, last_seen DESC") as c:
            targets = await c.fetchall()

    rows = ""
    for t in targets:
        cls = "online" if t['last_status'] == 'online' else "offline"
        ts = fmt_time(t['last_seen'])
        ident = t['tg_username'] if t['tg_username'] else t['tg_id']
        rows += f"""
        <div class="card grid-item" onclick="location.href='/target/{t['id']}'" style="cursor:pointer;">
            <div>
                <div style="font-size:1.1rem; font-weight:bold; color:#fff;">{t['display_name']}</div>
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
    <div style="display:flex; justify-content:space-between; align-items:center;">
        <h2 style="margin:0;">TARGET GRID</h2>
        <a href="/add"><button class="btn-small">+ NEW TARGET</button></a>
    </div>
    {rows}
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/target/<int:t_id>')
@login_required
async def target_detail(t_id):
    async with await get_db() as db:
        async with db.execute("SELECT * FROM targets WHERE id=?", (t_id,)) as c: target = await c.fetchone()
        async with db.execute("SELECT * FROM sessions WHERE target_id=? ORDER BY id DESC LIMIT 20", (t_id,)) as c: sessions = await c.fetchall()
    if not target: return "Target Not Found"
    heatmap_data = await get_heatmap_data(t_id)
    session_rows = ""
    for s in sessions:
        dur = s['duration'] if s['duration'] else "Active"
        session_rows += f"<tr><td>{fmt_time(s['start_time'])}</td><td>{fmt_time(s['end_time'])}</td><td>{dur}</td></tr>"

    content = f"""
    <div style="margin-bottom:15px;"><a href="/dashboard" style="color:#888;">&larr; BACK TO GRID</a></div>
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
    <div class="card"><h2>ACTIVITY HEATMAP (24H)</h2><div class="chart-container"><canvas id="activityChart"></canvas></div></div>
    <div class="card"><h2>RECENT SESSIONS</h2><table><tr><th>ONLINE</th><th>OFFLINE</th><th>DURATION</th></tr>{session_rows}</table></div>
    <script>
        const ctx = document.getElementById('activityChart');
        new Chart(ctx, {{
            type: 'bar',
            data: {{
                labels: Array.from({{length: 24}}, (_, i) => i + ":00"),
                datasets: [{{ label: 'Activity', data: {heatmap_data}, backgroundColor: 'rgba(0, 243, 255, 0.5)', borderRadius: 4 }}]
            }},
            options: {{ responsive: true, maintainAspectRatio: false, scales: {{ y: {{ beginAtZero: true, grid: {{ color: '#333' }} }}, x: {{ grid: {{ display: false }} }} }} }}
        }});
    </script>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/add', methods=['GET', 'POST'])
@login_required
async def add():
    if request.method == 'POST':
        f = await request.form
        tg_in = f.get('tg_input')
        tg_id = int(tg_in) if tg_in.isdigit() else 0
        tg_user = tg_in if not tg_in.isdigit() else None
        async with await get_db() as db:
            await db.execute("INSERT INTO targets (tg_id, tg_username, display_name, last_status) VALUES (?,?,?, 'unknown')", (tg_id, tg_user, f.get('name')))
            await db.commit()
        return redirect('/dashboard')
    content = """<div class="card"><h2>ADD TARGET</h2><form method="POST"><label>Name</label><input name="name" required><label>Telegram ID or Username (@handle)</label><input name="tg_input" required><button type="submit">INITIATE TRACKING</button></form></div>"""
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/delete/<int:t_id>')
@login_required
async def delete_target(t_id):
    async with await get_db() as db:
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

# --- TELEGRAM CONNECT (FIXED UI) ---
@app.route('/connect', methods=['GET', 'POST'])
@login_required
async def connect():
    global temp_client, phone_number, phone_code_hash, runtime_api_id, runtime_api_hash
    msg = ""
    
    # Pre-fill if known
    val_aid = runtime_api_id if runtime_api_id else ""
    val_hash = runtime_api_hash if runtime_api_hash else ""
    
    if request.method == 'POST':
        f = await request.form
        phone = f.get('phone')
        aid = f.get('api_id')
        ahash = f.get('api_hash')
        
        # Save credentials
        if aid and ahash:
            runtime_api_id = int(aid)
            runtime_api_hash = ahash
            await save_setting('api_id', aid)
            await save_setting('api_hash', ahash)
            
        try:
            temp_client = TelegramClient(StringSession(), runtime_api_id, runtime_api_hash)
            await temp_client.connect()
            send = await temp_client.send_code_request(phone)
            phone_number = phone
            phone_code_hash = send.phone_code_hash
            return redirect('/verify')
        except Exception as e: msg = f"Error: {e}"

    content = f"""
    <div class="card">
        <h2>LINK UPLINK</h2>
        <div style="color:red">{msg}</div>
        <form method="POST">
            <label>API ID (from my.telegram.org)</label>
            <input name="api_id" value="{val_aid}" required>
            <label>API HASH (from my.telegram.org)</label>
            <input name="api_hash" value="{val_hash}" required>
            <label>Phone Number</label>
            <input name="phone" placeholder="+91..." required>
            <button>SEND OTP</button>
        </form>
    </div>
    """
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
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/logout')
async def logout():
    session.clear()
    return redirect('/login')

@app.before_serving
async def startup():
    await init_db()
    asyncio.create_task(cyber_bot.start())

if __name__ == "__main__":
    config = Config()
    config.bind = [f"0.0.0.0:{os.getenv('PORT', '8000')}"]
    asyncio.run(serve(app, config))
