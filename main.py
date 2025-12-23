import asyncio
import logging
import os
import sys
import secrets
import time
import random
import json
from functools import wraps
from datetime import datetime
import aiosqlite
import pytz
from quart import Quart, render_template_string, request, redirect, url_for, session, abort, Response, jsonify
from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession
from telethon.tl.types import UserStatusOnline, UserStatusRecently, UserStatusOffline
from hypercorn.config import Config
from hypercorn.asyncio import serve

# --- ADVANCED CONFIGURATION ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
TZ = pytz.timezone('Asia/Kolkata')
ENABLE_REGISTRATION = True

# --- ENHANCED LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(name)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("Netrunner.Core")

# --- APP FACTORY ---
app = Quart(__name__)
app.secret_key = SECRET_KEY
bot_client = None

# --- DATABASE SCHEMA ---
DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE,
    password TEXT,
    is_admin BOOLEAN DEFAULT 0,
    status TEXT DEFAULT 'pending',
    last_login DATETIME
);

CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER,
    tg_id INTEGER,
    tg_username TEXT,
    phone TEXT,
    display_name TEXT,
    notes TEXT,
    last_status TEXT,
    last_seen DATETIME,
    is_tracking BOOLEAN DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER,
    event_type TEXT,
    timestamp DATETIME,
    metadata TEXT
);
"""

# --- UI LAYER ---
HTML_BASE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NETRUNNER // SYSTEM_V3</title>
    <style>
        :root {
            --bg: #020202;
            --panel: rgba(10, 10, 10, 0.95);
            --neon-green: #0f0;
            --neon-red: #f00;
            --neon-blue: #00f3ff;
            --scanline: rgba(0, 255, 0, 0.1);
        }
        * { box-sizing: border-box; font-family: 'Courier New', monospace; }
        body { background: var(--bg); color: #e0e0e0; margin: 0; min-height: 100vh; display: flex; flex-direction: column; overflow-x: hidden; }
        
        #matrix-canvas {
            position: fixed; top: 0; left: 0; width: 100%; height: 100%; z-index: 0; opacity: 0.15; pointer-events: none;
        }

        body::after {
            content: "";
            position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
            background: repeating-linear-gradient(0deg, var(--scanline), var(--scanline) 1px, transparent 1px, transparent 2px);
            pointer-events: none; z-index: 99; opacity: 0.5;
        }

        .container { max-width: 900px; margin: 20px auto; width: 100%; position: relative; z-index: 10; }
        
        .hud-bar {
            display: flex; justify-content: space-between; border-bottom: 2px solid var(--neon-green);
            padding: 10px; background: #000; color: var(--neon-green); text-transform: uppercase; letter-spacing: 2px;
        }
        
        .sys-clock { font-weight: bold; text-shadow: 0 0 5px var(--neon-green); }

        .card { 
            background: var(--panel); border: 1px solid #333; padding: 20px; margin-top: 20px; 
            box-shadow: 0 0 10px rgba(0,0,0,0.8); position: relative; overflow: hidden;
            transition: transform 0.2s;
        }
        .card:hover { border-color: var(--neon-blue); }
        
        .status-badge { padding: 2px 8px; border: 1px solid currentColor; font-weight: bold; }
        .online { color: var(--neon-green); box-shadow: 0 0 5px var(--neon-green); }
        .offline { color: var(--neon-red); }

        input, button { width: 100%; padding: 12px; margin-top: 10px; background: #000; border: 1px solid #333; color: var(--neon-blue); outline: none; transition: 0.3s; }
        input:focus { border-color: var(--neon-green); box-shadow: 0 0 8px rgba(0, 255, 0, 0.2); }
        button { cursor: pointer; font-weight: bold; text-transform: uppercase; }
        button:hover { background: var(--neon-green); color: #000; box-shadow: 0 0 15px var(--neon-green); }
        
        .btn-small { width: auto; display: inline-block; padding: 5px 10px; font-size: 0.7rem; margin-top: 0; }
        .btn-red { background: rgba(255,0,0,0.2); border-color: var(--neon-red); color: var(--neon-red); }
        .btn-red:hover { background: var(--neon-red); color: #000; }

        #console-output { color: #888; font-size: 0.8rem; margin-top: 20px; border-top: 1px dashed #333; padding-top: 10px; }
        
        table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
        th { text-align: left; color: var(--neon-blue); border-bottom: 1px solid var(--neon-blue); padding: 5px; }
        td { padding: 8px 5px; border-bottom: 1px solid #222; color: #ccc; }
        
        a { color: var(--neon-blue); text-decoration: none; }
    </style>
    <script>
        function initMatrix() {
            const canvas = document.getElementById('matrix-canvas');
            const ctx = canvas.getContext('2d');
            canvas.width = window.innerWidth;
            canvas.height = window.innerHeight;
            const alphabet = 'アァカサタナハマヤャラワガザダバパイィキシチニヒミリヂビピ0123456789';
            const fontSize = 16;
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
            setInterval(draw, 30);
        }

        const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        function playSound(type) {
            if (audioCtx.state === 'suspended') audioCtx.resume();
            const osc = audioCtx.createOscillator();
            const gain = audioCtx.createGain();
            osc.connect(gain);
            gain.connect(audioCtx.destination);
            
            if (type === 'hover') {
                osc.frequency.value = 400;
                gain.gain.exponentialRampToValueAtTime(0.00001, audioCtx.currentTime + 0.1);
                osc.start(); osc.stop(audioCtx.currentTime + 0.1);
            } else if (type === 'click') {
                osc.frequency.value = 800;
                gain.gain.exponentialRampToValueAtTime(0.00001, audioCtx.currentTime + 0.15);
                osc.start(); osc.stop(audioCtx.currentTime + 0.15);
            }
        }

        function updateClock() {
            const now = new Date();
            document.getElementById('sys-clock').innerText = now.toISOString().split('.')[0] + " Z";
        }

        document.addEventListener("DOMContentLoaded", () => {
            initMatrix();
            setInterval(updateClock, 1000);
            document.querySelectorAll('button').forEach(b => {
                b.addEventListener('mouseenter', () => playSound('hover'));
                b.addEventListener('click', () => playSound('click'));
            });
        });
    </script>
</head>
<body>
    <canvas id="matrix-canvas"></canvas>
    <div class="container">
        <div class="hud-bar">
            <span>NETRUNNER // ULTIMATE</span>
            <span id="sys-clock" class="sys-clock">LOADING...</span>
        </div>
        {{ CONTENT }}
        <div id="console-output"></div>
    </div>
</body>
</html>
"""

# --- MIDDLEWARE: SECURITY HEADERS ---
@app.after_request
async def add_security_headers(response: Response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    return response

# --- DATABASE ENGINE (FIXED) ---
# Custom Context Manager to prevent double-initialization of threads
class DbContext:
    def __init__(self):
        self.conn = None
    
    async def __aenter__(self):
        # We only connect when entering the block
        self.conn = await aiosqlite.connect('tracker.db')
        self.conn.row_factory = aiosqlite.Row
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        # We ensure it closes when leaving the block
        if self.conn:
            await self.conn.close()

# Helper function compatible with "await get_db()" syntax
async def get_db():
    return DbContext()

async def init_db():
    # Fixed usage: standard connection for initialization
    async with aiosqlite.connect('tracker.db') as db:
        await db.executescript(DB_SCHEMA)
        
        try:
            await db.execute("SELECT tg_username FROM targets LIMIT 1")
        except Exception:
            logger.info("Migrating Database: Adding 'tg_username' column...")
            await db.execute("ALTER TABLE targets ADD COLUMN tg_username TEXT")
        
        await db.commit()

        async with db.execute("SELECT * FROM users WHERE username = ?", (ADMIN_USER,)) as cursor:
            if not await cursor.fetchone():
                await db.execute("INSERT INTO users (username, password, is_admin, status) VALUES (?, ?, 1, 'active')", (ADMIN_USER, ADMIN_PASS))
                await db.commit()
                print(f"\n{'='*40}\n[ADMIN CREDENTIALS]\nUSER: {ADMIN_USER}\nPASS: {ADMIN_PASS}\n{'='*40}\n")

# --- SOPHISTICATED TRACKER ---
class CyberTracker:
    def __init__(self):
        self.client = None
        self.tracking_active = False

    async def start(self):
        if not SESSION_STRING:
            logger.critical("SESSION_STRING missing. Surveillance modules disabled.")
            return

        backoff = 2
        while True:
            try:
                self.client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
                await self.client.start()
                logger.info(f"Uplink Established: {await self.client.get_me()}")
                self.tracking_active = True
                asyncio.create_task(self.loop())
                break
            except Exception as e:
                logger.error(f"Connection Failed: {e}. Retrying in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def loop(self):
        while self.tracking_active:
            try:
                # Fixed: using the new DbContext via get_db() logic
                async with await get_db() as db:
                    async with db.execute("SELECT id, tg_id, tg_username, last_status FROM targets WHERE is_tracking = 1") as cursor:
                        targets = await cursor.fetchall()
                    
                    for row in targets:
                        await self._check_target(db, row)
                        await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Loop Error: {e}")
            
            await asyncio.sleep(10)

    async def _check_target(self, db, row):
        t_id = row['id']
        last_status = row['last_status']
        
        target_identifier = row['tg_id']
        if (not target_identifier or target_identifier == 0) and row['tg_username']:
            target_identifier = row['tg_username']

        if not target_identifier: return

        try:
            entity = await self.client.get_entity(target_identifier)
            
            if isinstance(target_identifier, str) and entity.id:
                 await db.execute("UPDATE targets SET tg_id = ? WHERE id = ?", (entity.id, t_id))

            status = entity.status
            new_status = 'offline'
            if isinstance(status, UserStatusOnline): new_status = 'online'
            elif isinstance(status, UserStatusRecently): new_status = 'recently'
            
            if new_status != last_status:
                now = datetime.now(TZ)
                await db.execute("UPDATE targets SET last_status = ?, last_seen = ? WHERE id = ?", (new_status, now, t_id))
                await db.execute("INSERT INTO logs (target_id, event_type, timestamp) VALUES (?, ?, ?)", (t_id, new_status.upper(), now))
                await db.commit()
        except Exception: pass

cyber_bot = CyberTracker()

# --- ACCESS CONTROL DECORATORS ---
def login_required(f):
    @wraps(f)
    async def decorated_function(*args, **kwargs):
        if 'user_id' not in session: return redirect(url_for('login'))
        return await f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    async def decorated_function(*args, **kwargs):
        if not session.get('is_admin'): abort(403)
        return await f(*args, **kwargs)
    return decorated_function

# --- ROUTES ---
@app.route('/')
async def index():
    return redirect(url_for('dashboard')) if 'user_id' in session else redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
async def login():
    error_msg = ""
    if request.method == 'POST':
        form = await request.form
        async with await get_db() as db:
            cursor = await db.execute("SELECT id, is_admin, status FROM users WHERE username = ? AND password = ?", (form.get('username'), form.get('password')))
            user = await cursor.fetchone()
        
        if user:
            if user['status'] != 'active': error_msg = "ACCOUNT_LOCKED"
            else:
                session['user_id'] = user['id']
                session['is_admin'] = bool(user['is_admin'])
                async with await get_db() as db:
                    await db.execute("UPDATE users SET last_login = ? WHERE id = ?", (datetime.now(), user['id']))
                    await db.commit()
                return redirect(url_for('dashboard'))
        else:
            error_msg = "INVALID_CREDENTIALS"

    register_html = f'<p style="text-align:center; margin-top:15px; font-size:0.7rem;">NO ID? <a href="/register">CREATE_IDENTITY</a></p>' if ENABLE_REGISTRATION else ''

    content = f"""
    <div class="card" style="max-width:400px; margin:50px auto; border-color: var(--neon-blue);">
        <h2 style="text-align:center; color:var(--neon-blue);">AUTHENTICATION</h2>
        {f'<div style="color:var(--neon-red); text-align:center; margin-bottom:10px;">[ERROR: {error_msg}]</div>' if error_msg else ''}
        <form method="POST">
            <label style="font-size:0.7rem; color:#666;">AGENT ID</label>
            <input type="text" name="username" required autocomplete="off">
            <label style="font-size:0.7rem; color:#666; margin-top:10px; display:block;">ACCESS KEY</label>
            <input type="password" name="password" required>
            <button type="submit" style="margin-top:20px;">CONNECT</button>
        </form>
        {register_html}
    </div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/register', methods=['GET', 'POST'])
async def register():
    if not ENABLE_REGISTRATION: return "REGISTRATION_DISABLED"
    
    error_msg = ""
    if request.method == 'POST':
        form = await request.form
        username = form.get('username')
        password = form.get('password')
        
        if len(password) < 4:
            error_msg = "PASSWORD_TOO_WEAK"
        else:
            try:
                async with await get_db() as db:
                    await db.execute("INSERT INTO users (username, password, status) VALUES (?, ?, 'active')", (username, password))
                    await db.commit()
                return redirect(url_for('login'))
            except Exception:
                error_msg = "USERNAME_TAKEN"

    content = f"""
    <div class="card" style="max-width:400px; margin:50px auto;">
        <h2 style="text-align:center; color:var(--neon-green);">NEW IDENTITY</h2>
        {f'<div style="color:var(--neon-red); text-align:center;">{error_msg}</div>' if error_msg else ''}
        <form method="POST">
            <label>DESIRED HANDLE</label>
            <input type="text" name="username" required>
            <label>PASSPHRASE</label>
            <input type="password" name="password" required>
            <button type="submit">GENERATE</button>
        </form>
        <p style="text-align:center;"><a href="/login">RETURN TO LOGIN</a></p>
    </div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/dashboard')
@login_required
async def dashboard():
    uid = session['user_id']
    targets = []
    async with await get_db() as db:
        if session['is_admin']:
            query = "SELECT t.*, u.username as owner_name FROM targets t LEFT JOIN users u ON t.owner_id = u.id"
            args = ()
        else:
            query = "SELECT * FROM targets WHERE owner_id = ?"
            args = (uid,)
        async with db.execute(query, args) as c: targets = await c.fetchall()

    target_cards = ""
    for t in targets:
        status_class = "online" if t['last_status'] == 'online' else "offline"
        tg_display = t['tg_username'] if t['tg_username'] else t['tg_id']
        
        target_cards += f"""
        <div class="card">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <span style="font-size: 1.2rem; color: #fff;">{t['display_name']}</span>
                <span class="status-badge {status_class}">{t['last_status'].upper()}</span>
            </div>
            <div style="margin-top: 10px; font-size: 0.85rem; color: #aaa;">
                <div style="color:var(--neon-blue);">HANDLE: {tg_display}</div>
                <div>PHONE: {t['phone'] or 'N/A'}</div>
                <div>LAST SEEN: {t['last_seen'] or 'NEVER'}</div>
            </div>
            <div style="margin-top:15px; text-align:right;">
                <a href="/edit/{t['id']}" class="btn-small" style="border:1px solid #666; color:#fff;">EDIT / UPDATE</a>
            </div>
        </div>
        """

    content = f"""
    <div style="display:flex; gap:10px; margin-bottom:20px; flex-wrap:wrap;">
        <a href="/dashboard"><button style="background:#222; width:auto;">GRID VIEW</button></a>
        { '<a href="/admin"><button style="background:var(--neon-red); color:#fff; width:auto;">ADMIN_CORE</button></a>' if session.get('is_admin') else '' }
        { '<a href="/logs"><button style="background:var(--neon-blue); color:#000; width:auto;">SYS_LOGS</button></a>' if session.get('is_admin') else '' }
        <a href="/logout"><button style="width:auto;">TERMINATE SESSION</button></a>
    </div>

    <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 15px;">
        {target_cards}
        <div class="card" style="border-style: dashed; display: flex; align-items: center; justify-content: center; opacity:0.6; cursor: pointer; min-height:150px;" onclick="location.href='/add'">
            <div style="text-align:center;">
                <div style="font-size: 3rem;">+</div>
                <div>NEW_TARGET</div>
            </div>
        </div>
    </div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/add', methods=['GET', 'POST'])
@login_required
async def add_target():
    if request.method == 'POST':
        form = await request.form
        
        tg_input = form.get('tg_input')
        tg_id = 0
        tg_username = None

        if tg_input.startswith('@') or not tg_input.isdigit():
            tg_username = tg_input
        else:
            tg_id = int(tg_input)

        async with await get_db() as db:
            await db.execute(
                "INSERT INTO targets (owner_id, tg_id, tg_username, phone, display_name, last_status) VALUES (?, ?, ?, ?, ?, 'unknown')", 
                (session['user_id'], tg_id, tg_username, form.get('phone'), form.get('name'))
            )
            await db.commit()
        return redirect(url_for('dashboard'))

    content = """
    <div class="card" style="max-width:500px; margin:0 auto;">
        <h3>INITIALIZE TRACKING SEQUENCE</h3>
        <form method="POST">
            <label>DISPLAY NAME (ALIAS)</label>
            <input type="text" name="name" required placeholder="e.g. Target Alpha">
            
            <label style="color:var(--neon-blue);">TELEGRAM USERNAME OR ID</label>
            <input type="text" name="tg_input" required placeholder="@username OR 123456789">
            
            <label>PHONE NUMBER (OPTIONAL)</label>
            <input type="text" name="phone" placeholder="+1...">
            
            <div style="display:flex; gap:10px; margin-top:20px;">
                <button type="submit">EXECUTE</button>
                <button type="button" onclick="history.back()" style="background:#333; color:#fff;">ABORT</button>
            </div>
        </form>
    </div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/edit/<int:t_id>', methods=['GET', 'POST'])
@login_required
async def edit_target(t_id):
    async with await get_db() as db:
        query = "SELECT * FROM targets WHERE id = ?"
        async with db.execute(query, (t_id,)) as c:
            target = await c.fetchone()
            
    if not target: return "TARGET_NOT_FOUND"
    if not session['is_admin'] and target['owner_id'] != session['user_id']:
        return "ACCESS_DENIED"

    if request.method == 'POST':
        form = await request.form
        
        tg_input = form.get('tg_input')
        new_tg_id = target['tg_id']
        new_tg_username = target['tg_username']
        
        if tg_input.startswith('@') or not tg_input.isdigit():
            new_tg_username = tg_input
        else:
            new_tg_id = int(tg_input)

        async with await get_db() as db:
            await db.execute("""
                UPDATE targets 
                SET display_name = ?, phone = ?, tg_id = ?, tg_username = ? 
                WHERE id = ?
            """, (form.get('name'), form.get('phone'), new_tg_id, new_tg_username, t_id))
            await db.commit()
        return redirect(url_for('dashboard'))

    current_tg_val = target['tg_username'] if target['tg_username'] else target['tg_id']

    content = f"""
    <div class="card" style="max-width:500px; margin:0 auto; border-color:var(--neon-blue);">
        <h3>MODIFY TRACKING DATA</h3>
        <form method="POST">
            <label>DISPLAY NAME</label>
            <input type="text" name="name" value="{target['display_name']}" required>
            
            <label style="color:var(--neon-blue);">TELEGRAM USERNAME OR ID</label>
            <input type="text" name="tg_input" value="{current_tg_val}" required>
            
            <label>PHONE NUMBER</label>
            <input type="text" name="phone" value="{target['phone'] or ''}">
            
            <div style="display:flex; gap:10px; margin-top:20px;">
                <button type="submit">UPDATE RECORD</button>
                <button type="button" onclick="history.back()" style="background:#333; color:#fff;">CANCEL</button>
            </div>
        </form>
    </div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/admin')
@admin_required
async def admin_panel():
    async with await get_db() as db:
        async with db.execute("SELECT * FROM users") as c: users = await c.fetchall()

    user_rows = ""
    for u in users:
        user_rows += f"""
        <tr>
            <td>{u['id']}</td>
            <td>{u['username']}</td>
            <td style="color: {'var(--neon-green)' if u['status']=='active' else 'var(--neon-red)'}">{u['status']}</td>
            <td>{u['last_login'] or 'NEVER'}</td>
        </tr>
        """

    content = f"""
    <div class="card">
        <h3>ROOT_ACCESS // USER_DB</h3>
        <table>
            <tr><th>UID</th><th>HANDLE</th><th>STATE</th><th>LAST_LOGIN</th></tr>
            {user_rows}
        </table>
        <div style="margin-top:20px;">
            <button onclick="location.href='/dashboard'">RETURN TO GRID</button>
        </div>
    </div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/logs')
@admin_required
async def view_logs():
    async with await get_db() as db:
        query = """SELECT l.event_type, l.timestamp, t.display_name FROM logs l 
                   LEFT JOIN targets t ON l.target_id = t.id ORDER BY l.timestamp DESC LIMIT 50"""
        async with db.execute(query) as c: logs = await c.fetchall()

    log_rows = ""
    for l in logs:
        log_rows += f"<tr><td>{l['timestamp']}</td><td>{l['display_name']}</td><td>{l['event_type']}</td></tr>"

    content = f"""
    <div class="card"><h3>SYSTEM_EVENT_LOGS</h3><table><tr><th>TIME</th><th>TARGET</th><th>EVENT</th></tr>{log_rows}</table>
    <button onclick="history.back()" style="margin-top:20px;">BACK</button></div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/api/status')
async def api_status():
    async with await get_db() as db:
        async with db.execute("SELECT COUNT(*) as c FROM targets WHERE last_status = 'online'") as c:
            online = (await c.fetchone())['c']
    return jsonify({"status": "OPERATIONAL", "online_targets": online})

@app.route('/logout')
async def logout():
    session.clear()
    return redirect(url_for('login'))

@app.before_serving
async def startup():
    await init_db()
    asyncio.create_task(cyber_bot.start())

@app.after_serving
async def shutdown():
    if cyber_bot.client: await cyber_bot.client.disconnect()

if __name__ == "__main__":
    config = Config()
    config.bind = [f"0.0.0.0:{os.getenv('PORT', '8000')}"]
    asyncio.run(serve(app, config))
