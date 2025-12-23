import asyncio  # FIXED: Capital 'Import' caused SyntaxError
import logging
import os
import sys
import secrets
from functools import wraps
from datetime import datetime
import aiosqlite
import pytz
from quart import Quart, render_template_string, request, redirect, url_for, session, abort
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import UserStatusOnline, UserStatusRecently
from hypercorn.config import Config
from hypercorn.asyncio import serve

# --- CONFIGURATION (ENV VARS) ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(16))
TZ = pytz.timezone('Asia/Kolkata')

# --- LOGGING SETUP ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("CyberBot")

# --- APP INITIALIZATION ---
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
    status TEXT DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER,
    tg_id INTEGER,
    phone TEXT,
    display_name TEXT,
    notes TEXT,
    last_status TEXT,
    last_seen DATETIME,
    is_tracking BOOLEAN DEFAULT 1
);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER,
    event_type TEXT,
    timestamp DATETIME
);
"""

# --- CYBERPUNK UI BASE (The "Layout") ---
# IMPORTANT: We use {{ CONTENT }} to avoid Jinja2 collisions
HTML_BASE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NETRUNNER // V1</title>
    <style>
        :root {
            --bg: #050505;
            --panel: #0a0a0a;
            --neon-green: #00ff41;
            --neon-red: #ff0055;
            --neon-blue: #00f3ff;
            --text-main: #e0e0e0;
            --text-dim: #666;
            --border: 1px solid #333;
        }
        * { box-sizing: border-box; font-family: 'Courier New', monospace; }
        body { background: var(--bg); color: var(--text-main); margin: 0; padding: 0; overflow-x: hidden; }
        
        /* SCANLINE EFFECT */
        body::before {
            content: " ";
            display: block;
            position: absolute;
            top: 0; left: 0; bottom: 0; right: 0;
            background: linear-gradient(rgba(18, 16, 16, 0) 50%, rgba(0, 0, 0, 0.25) 50%), linear-gradient(90deg, rgba(255, 0, 0, 0.06), rgba(0, 255, 0, 0.02), rgba(0, 0, 255, 0.06));
            z-index: 2;
            background-size: 100% 2px, 3px 100%;
            pointer-events: none;
        }

        .container { max-width: 800px; margin: 0 auto; padding: 20px; z-index: 3; position: relative; }
        
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid var(--neon-green); padding-bottom: 10px; margin-bottom: 20px; }
        .logo { font-size: 1.5rem; font-weight: bold; color: var(--neon-green); text-shadow: 0 0 5px var(--neon-green); }
        .user-badge { font-size: 0.8rem; color: var(--neon-blue); }

        .card { background: var(--panel); border: 1px solid #333; padding: 15px; margin-bottom: 15px; position: relative; }
        .card::after { content: ''; position: absolute; top: 0; right: 0; width: 0; height: 0; border-style: solid; border-width: 0 20px 20px 0; border-color: transparent var(--neon-green) transparent transparent; }
        
        .status-online { color: var(--neon-green); font-weight: bold; animation: pulse 2s infinite; }
        .status-offline { color: var(--neon-red); }
        
        @keyframes pulse {
            0% { opacity: 1; text-shadow: 0 0 5px var(--neon-green); }
            50% { opacity: 0.5; text-shadow: 0 0 2px var(--neon-green); }
            100% { opacity: 1; text-shadow: 0 0 5px var(--neon-green); }
        }

        input, select { background: #000; border: 1px solid var(--neon-blue); color: #fff; padding: 10px; width: 100%; margin-bottom: 10px; }
        button { background: var(--neon-green); color: #000; border: none; padding: 10px 20px; font-weight: bold; cursor: pointer; text-transform: uppercase; width: 100%; }
        button:hover { background: #fff; box-shadow: 0 0 10px var(--neon-green); }
        
        table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
        th { text-align: left; color: var(--neon-blue); border-bottom: 1px solid var(--neon-blue); padding: 5px; }
        td { padding: 8px 5px; border-bottom: 1px solid #222; }

        .nav { display: flex; gap: 10px; margin-bottom: 20px; }
        .nav a { color: var(--text-dim); text-decoration: none; padding: 5px 10px; border: 1px solid transparent; }
        .nav a.active { color: var(--neon-green); border-color: var(--neon-green); }
    </style>
</head>
<body>
    <div class="container">
        {{ CONTENT }}
    </div>
</body>
</html>
"""

# --- DATABASE HELPERS ---
async def init_db():
    async with aiosqlite.connect('tracker.db') as db:
        await db.executescript(DB_SCHEMA)
        await db.commit()
        async with db.execute("SELECT * FROM users WHERE username = ?", (ADMIN_USER,)) as cursor:
            if not await cursor.fetchone():
                await db.execute("INSERT INTO users (username, password, is_admin, status) VALUES (?, ?, 1, 'active')", (ADMIN_USER, ADMIN_PASS))
                await db.commit()

# --- BOT LOGIC ---
class CyberTracker:
    def __init__(self):
        self.client = None
        self.tracking_active = False

    async def start(self):
        if not SESSION_STRING:
            logger.warning("No SESSION_STRING. Bot disabled.")
            return
        try:
            self.client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
            await self.client.start()
            logger.info(f"Bot Connected as: {await self.client.get_me()}")
            self.tracking_active = True
            asyncio.create_task(self.loop())
        except Exception as e:
            logger.error(f"Bot Start Failed: {e}")

    async def loop(self):
        while self.tracking_active:
            try:
                async with aiosqlite.connect('tracker.db') as db:
                    async with db.execute("SELECT id, tg_id, last_status FROM targets WHERE is_tracking = 1") as cursor:
                        targets = await cursor.fetchall()
                    
                    for row in targets:
                        t_id, tg_id, last_status = row
                        try:
                            entity = await self.client.get_entity(tg_id)
                            status = entity.status
                            new_status = 'online' if isinstance(status, UserStatusOnline) else 'offline'
                            
                            if new_status != last_status:
                                now = datetime.now(TZ)
                                await db.execute("UPDATE targets SET last_status = ?, last_seen = ? WHERE id = ?", (new_status, now, t_id))
                                await db.execute("INSERT INTO logs (target_id, event_type, timestamp) VALUES (?, ?, ?)", (t_id, new_status.upper(), now))
                                await db.commit()
                        except: pass
            except: pass
            await asyncio.sleep(5)

cyber_bot = CyberTracker()

# --- DECORATORS ---
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
    if request.method == 'POST':
        form = await request.form
        async with aiosqlite.connect('tracker.db') as db:
            async with db.execute("SELECT id, is_admin, status FROM users WHERE username = ? AND password = ?", (form.get('username'), form.get('password'))) as c:
                user = await c.fetchone()
        
        if user:
            if user[2] != 'active': return "ACCOUNT LOCKED"
            session['user_id'] = user[0]
            session['is_admin'] = bool(user[1])
            return redirect(url_for('dashboard'))
        
        # ERROR RESPONSE
        content = """
        <div class="card" style="text-align:center;">
            <h2 class="status-offline">ACCESS DENIED</h2>
            <button onclick="history.back()">RETRY</button>
        </div>
        """
        # FIXED: Replaced empty string replacement with placeholder
        final_html = HTML_BASE.replace('{{ CONTENT }}', content)
        return await render_template_string(final_html)

    # LOGIN FORM
    content = """
    <div class="card" style="max-width:400px; margin:50px auto;">
        <h2 style="text-align:center; color:var(--neon-green);">SYSTEM LOGIN</h2>
        <form method="POST">
            <input type="text" name="username" placeholder="CODENAME" required>
            <input type="password" name="password" placeholder="PASSPHRASE" required>
            <button type="submit">AUTHENTICATE</button>
        </form>
    </div>
    """
    # FIXED: Replaced empty string replacement with placeholder
    final_html = HTML_BASE.replace('{{ CONTENT }}', content)
    return await render_template_string(final_html)

@app.route('/dashboard')
@login_required
async def dashboard():
    uid = session['user_id']
    targets = []
    async with aiosqlite.connect('tracker.db') as db:
        query = "SELECT id, display_name, last_status, last_seen, phone FROM targets" if session['is_admin'] else "SELECT id, display_name, last_status, last_seen, phone FROM targets WHERE owner_id = ?"
        args = () if session['is_admin'] else (uid,)
        async with db.execute(query, args) as c: targets = await c.fetchall()

    content = """
    <div class="header">
        <div class="logo">NETRUNNER_V1</div>
        <div class="user-badge">OP: {{ session.get('user_id') }} | <a href="/logout" style="color:var(--neon-red)">LOGOUT</a></div>
    </div>
    
    {% if session.get('is_admin') %}
    <div class="nav">
        <a href="/dashboard" class="active">MONITOR</a>
        <a href="/admin">ADMIN_PANEL</a>
    </div>
    {% endif %}

    <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 15px;">
        {% for target in targets %}
        <div class="card">
            <div style="display: flex; justify-content: space-between;">
                <strong style="font-size: 1.2rem;">{{ target[1] }}</strong>
                <span class="{{ 'status-online' if target[2] == 'online' else 'status-offline' }}">
                    {{ target[2].upper() }}
                </span>
            </div>
            <div style="font-size: 0.8rem; color: var(--text-dim); margin-top: 5px;">
                PHONE: {{ target[4] }}<br>
                LAST SEEN: {{ target[3] }}
            </div>
        </div>
        {% endfor %}
        <div class="card" style="border-style: dashed; display: flex; align-items: center; justify-content: center; cursor: pointer;" onclick="location.href='/add'">
            <span style="font-size: 2rem; color: var(--text-dim);">+</span>
        </div>
    </div>
    """
    
    # FIXED: Replaced empty string replacement with placeholder
    final_html = HTML_BASE.replace('{{ CONTENT }}', content)
    return await render_template_string(final_html, targets=targets, session=session)

@app.route('/add', methods=['GET', 'POST'])
@login_required
async def add_target():
    if request.method == 'POST':
        form = await request.form
        async with aiosqlite.connect('tracker.db') as db:
            await db.execute("INSERT INTO targets (owner_id, tg_id, phone, display_name, last_status) VALUES (?, ?, ?, ?, 'unknown')", 
                             (session['user_id'], form.get('tg_id'), form.get('phone'), form.get('name')))
            await db.commit()
        return redirect(url_for('dashboard'))

    content = """
    <div class="header"><div class="logo">ADD TARGET</div></div>
    <div class="card">
        <form method="POST">
            <label>DISPLAY NAME</label>
            <input type="text" name="name" placeholder="Alias" required>
            <label>TELEGRAM ID</label>
            <input type="number" name="tg_id" placeholder="Numeric ID" required>
            <label>PHONE (OPTIONAL)</label>
            <input type="text" name="phone" placeholder="+91...">
            <button type="submit">INITIATE TRACKING</button>
        </form>
        <button onclick="history.back()" style="background:#333; color:#fff;">CANCEL</button>
    </div>
    """
    # FIXED: Replaced empty string replacement with placeholder
    final_html = HTML_BASE.replace('{{ CONTENT }}', content)
    return await render_template_string(final_html)

@app.route('/admin')
@admin_required
async def admin_panel():
    users = []
    async with aiosqlite.connect('tracker.db') as db:
        async with db.execute("SELECT id, username, status FROM users") as c: users = await c.fetchall()

    content = """
    <div class="header">
        <div class="logo">ADMIN // GOD_MODE</div>
        <a href="/dashboard" style="color: var(--neon-blue);">BACK</a>
    </div>
    <div class="card">
        <h3>SYSTEM STATUS</h3>
        <p>BOT STATE: <span class="status-online">ONLINE</span></p>
        <p>DB CONNECTION: STABLE</p>
    </div>
    <div class="card">
        <h3>USER MANAGEMENT</h3>
        <table>
            <tr><th>ID</th><th>USER</th><th>STATUS</th></tr>
            {% for u in users %}
            <tr>
                <td>{{ u[0] }}</td>
                <td>{{ u[1] }}</td>
                <td style="color: {{ 'var(--neon-green)' if u[2]=='active' else 'var(--neon-red)' }}">{{ u[2].upper() }}</td>
            </tr>
            {% endfor %}
        </table>
    </div>
    """
    # FIXED: Replaced empty string replacement with placeholder
    final_html = HTML_BASE.replace('{{ CONTENT }}', content)
    return await render_template_string(final_html, users=users)

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
