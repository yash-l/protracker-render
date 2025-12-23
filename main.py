import asyncio
import logging
import os
import sys
import json
import secrets
import signal
from datetime import datetime, timedelta
from functools import wraps
import aiosqlite
import pytz
from quart import Quart, render_template_string, request, redirect, url_for, session, abort, jsonify
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import UserStatusOnline, UserStatusOffline, UserStatusRecently
from hypercorn.config import Config
from hypercorn.asyncio import serve

# --- CONFIGURATION (ENV VARS) ---
API_ID = int(os.getenv("API_ID", "0"))  # Replace with yours if local
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

# Global State
bot_client = None
shutdown_event = asyncio.Event()

# --- DATABASE SCHEMA (SQLite) ---
DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE,
    password TEXT,
    is_admin BOOLEAN DEFAULT 0,
    status TEXT DEFAULT 'pending' -- active, pending, banned
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
    event_type TEXT, -- ONLINE, OFFLINE
    timestamp DATETIME,
    duration INTEGER -- Seconds
);
"""

# --- CYBERPUNK UI TEMPLATES (HTML/CSS/JS) ---
HTML_BASE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NETRUNNER // TRACKER</title>
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
        
        /* HEADER */
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid var(--neon-green); padding-bottom: 10px; margin-bottom: 20px; }
        .logo { font-size: 1.5rem; font-weight: bold; color: var(--neon-green); text-shadow: 0 0 5px var(--neon-green); }
        .user-badge { font-size: 0.8rem; color: var(--neon-blue); }

        /* CARDS */
        .card { background: var(--panel); border: 1px solid #333; padding: 15px; margin-bottom: 15px; position: relative; }
        .card::after { content: ''; position: absolute; top: 0; right: 0; width: 0; height: 0; border-style: solid; border-width: 0 20px 20px 0; border-color: transparent var(--neon-green) transparent transparent; }
        
        /* GLITCH TEXT */
        .status-online { color: var(--neon-green); font-weight: bold; animation: pulse 2s infinite; }
        .status-offline { color: var(--neon-red); }
        
        @keyframes pulse {
            0% { opacity: 1; text-shadow: 0 0 5px var(--neon-green); }
            50% { opacity: 0.5; text-shadow: 0 0 2px var(--neon-green); }
            100% { opacity: 1; text-shadow: 0 0 5px var(--neon-green); }
        }

        /* FORMS & BUTTONS */
        input, select { background: #000; border: 1px solid var(--neon-blue); color: #fff; padding: 10px; width: 100%; margin-bottom: 10px; }
        button { background: var(--neon-green); color: #000; border: none; padding: 10px 20px; font-weight: bold; cursor: pointer; text-transform: uppercase; width: 100%; }
        button:hover { background: #fff; box-shadow: 0 0 10px var(--neon-green); }
        .btn-danger { background: var(--neon-red); color: #fff; }

        /* TABLES */
        table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
        th { text-align: left; color: var(--neon-blue); border-bottom: 1px solid var(--neon-blue); padding: 5px; }
        td { padding: 8px 5px; border-bottom: 1px solid #222; }

        /* NAV */
        .nav { display: flex; gap: 10px; margin-bottom: 20px; }
        .nav a { color: var(--text-dim); text-decoration: none; padding: 5px 10px; border: 1px solid transparent; }
        .nav a.active { color: var(--neon-green); border-color: var(--neon-green); }
        
        .hardware-monitor { font-size: 0.7rem; color: var(--text-dim); text-align: right; margin-top: -10px; margin-bottom: 20px; }
    </style>
</head>
<body>
    <div class="container">
        {% block content %}{% endblock %}
    </div>
</body>
</html>
"""

# --- DATABASE HELPERS ---
async def init_db():
    async with aiosqlite.connect('tracker.db') as db:
        await db.executescript(DB_SCHEMA)
        await db.commit()
        # Create Admin if not exists
        async with db.execute("SELECT * FROM users WHERE username = ?", (ADMIN_USER,)) as cursor:
            if not await cursor.fetchone():
                await db.execute("INSERT INTO users (username, password, is_admin, status) VALUES (?, ?, 1, 'active')", (ADMIN_USER, ADMIN_PASS))
                await db.commit()
                logger.info("Admin user created.")

# --- TELEGRAM BOT LOGIC ---
class CyberTracker:
    def __init__(self):
        self.client = None
        self.tracking_active = False

    async def start(self):
        if not SESSION_STRING:
            logger.warning("No SESSION_STRING found. Bot will not run.")
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
        logger.info("Tracking Loop Initiated...")
        while self.tracking_active:
            try:
                async with aiosqlite.connect('tracker.db') as db:
                    # Fetch all targets
                    async with db.execute("SELECT id, tg_id, last_status FROM targets WHERE is_tracking = 1") as cursor:
                        targets = await cursor.fetchall()
                    
                    for row in targets:
                        t_id, tg_id, last_status = row
                        try:
                            entity = await self.client.get_entity(tg_id)
                            status = entity.status
                            
                            new_status = 'offline'
                            if isinstance(status, UserStatusOnline):
                                new_status = 'online'
                            
                            # Update if changed
                            if new_status != last_status:
                                now = datetime.now(TZ)
                                await db.execute("UPDATE targets SET last_status = ?, last_seen = ? WHERE id = ?", (new_status, now, t_id))
                                
                                # Log Event
                                await db.execute("INSERT INTO logs (target_id, event_type, timestamp) VALUES (?, ?, ?)", (t_id, new_status.upper(), now))
                                await db.commit()
                                
                                # Smart Alert (Anti-Jitter would go here - simplified for single file)
                                if new_status == 'online':
                                    logger.info(f"Target {tg_id} came ONLINE.")
                        
                        except Exception as e:
                            logger.error(f"Error checking {tg_id}: {e}")
                            
            except Exception as e:
                logger.error(f"Loop Error: {e}")
            
            await asyncio.sleep(5) # Check every 5 seconds

cyber_bot = CyberTracker()

# --- WEB AUTH DECORATORS ---
def login_required(f):
    @wraps(f)
    async def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return await f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    async def decorated_function(*args, **kwargs):
        if not session.get('is_admin'):
            abort(403)
        return await f(*args, **kwargs)
    return decorated_function

# --- ROUTES ---

@app.route('/')
async def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
async def login():
    if request.method == 'POST':
        form = await request.form
        username = form.get('username')
        password = form.get('password')
        
        async with aiosqlite.connect('tracker.db') as db:
            async with db.execute("SELECT id, is_admin, status FROM users WHERE username = ? AND password = ?", (username, password)) as cursor:
                user = await cursor.fetchone()
                
        if user:
            if user[2] != 'active':
                return "ACCOUNT LOCKED. AWAITING ADMIN APPROVAL."
            session['user_id'] = user[0]
            session['is_admin'] = bool(user[1])
            return redirect(url_for('dashboard'))
        else:
            return render_template_string(HTML_BASE + """
            {% block content %}
            <div class="card" style="max-width: 400px; margin: 50px auto; text-align: center;">
                <h2 class="logo">ACCESS DENIED</h2>
                <p style="color: var(--neon-red);">Invalid Credentials</p>
                <button onclick="window.history.back()">RETRY</button>
            </div>
            {% endblock %}
            """)

    return render_template_string(HTML_BASE + """
    {% block content %}
    <div class="card" style="max-width: 400px; margin: 100px auto;">
        <h2 class="logo" style="text-align: center;">SYSTEM LOGIN</h2>
        <form method="POST">
            <input type="text" name="username" placeholder="CODENAME" required>
            <input type="password" name="password" placeholder="PASSPHRASE" required>
            <button type="submit">AUTHENTICATE</button>
        </form>
    </div>
    {% endblock %}
    """)

@app.route('/dashboard')
@login_required
async def dashboard():
    user_id = session['user_id']
    is_admin = session['is_admin']
    
    async with aiosqlite.connect('tracker.db') as db:
        # Fetch targets based on role
        if is_admin:
            query = "SELECT id, display_name, last_status, last_seen, phone FROM targets"
            args = ()
        else:
            query = "SELECT id, display_name, last_status, last_seen, phone FROM targets WHERE owner_id = ?"
            args = (user_id,)
            
        async with db.execute(query, args) as cursor:
            targets = await cursor.fetchall()

    html = """
    {% extends "layout.html" %}
    {% block content %}
    <div class="header">
        <div class="logo">NETRUNNER_V1</div>
        <div class="user-badge">OP: {{ session.get('user_id') }} | <a href="/logout">LOGOUT</a></div>
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
            <div style="margin-top: 10px; border-top: 1px dashed #333; padding-top: 5px;">
                <a href="/report/{{ target[0] }}" style="color: var(--neon-blue); text-decoration: none;">[ VIEW_INTEL ]</a>
            </div>
        </div>
        {% endfor %}
        
        <div class="card" style="border-style: dashed; display: flex; align-items: center; justify-content: center; cursor: pointer;" onclick="location.href='/add'">
            <span style="font-size: 2rem; color: var(--text-dim);">+</span>
        </div>
    </div>
    {% endblock %}
    """
    # Quick hack to inject the base template for rendering
    full_template = html.replace('{% extends "layout.html" %}', HTML_BASE.replace('{% block content %}{% endblock %}', '{% block content %}REPLACE_ME{% endblock %}'))
    full_template = full_template.replace('REPLACE_ME', html.split('{% block content %}')[1].split('{% endblock %}')[0])
    
    return render_template_string(full_template, targets=targets, session=session)

@app.route('/add', methods=['GET', 'POST'])
@login_required
async def add_target():
    if request.method == 'POST':
        tg_id = request.form.get('tg_id')
        phone = request.form.get('phone')
        name = request.form.get('name')
        
        # Validation Logic (Dual Input Check)
        # For this version, we trust the ID, but save the phone for display
        
        async with aiosqlite.connect('tracker.db') as db:
            await db.execute("INSERT INTO targets (owner_id, tg_id, phone, display_name, last_status) VALUES (?, ?, ?, ?, 'unknown')", 
                             (session['user_id'], tg_id, phone, name))
            await db.commit()
        return redirect(url_for('dashboard'))

    return render_template_string(HTML_BASE + """
    {% block content %}
    <div class="header"><div class="logo">ADD TARGET</div></div>
    <div class="card">
        <form method="POST">
            <label>DISPLAY NAME (ALIAS)</label>
            <input type="text" name="name" placeholder="e.g. Arasaka Agent" required>
            
            <label>TELEGRAM USER ID (NUMERIC)</label>
            <input type="number" name="tg_id" placeholder="e.g. 12345678" required>
            
            <label>PHONE NUMBER (FOR VERIFICATION)</label>
            <input type="text" name="phone" placeholder="+91..." required>
            
            <button type="submit">INITIATE TRACKING</button>
        </form>
        <br>
        <button onclick="window.history.back()" style="background: #333; color: #fff;">CANCEL</button>
    </div>
    {% endblock %}
    """)

@app.route('/logout')
async def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/admin')
@admin_required
async def admin_panel():
    async with aiosqlite.connect('tracker.db') as db:
        async with db.execute("SELECT id, username, status FROM users") as cursor:
            users = await cursor.fetchall()
            
    return render_template_string(HTML_BASE + """
    {% block content %}
    <div class="header">
        <div class="logo">ADMIN_PANEL // GOD_MODE</div>
        <a href="/dashboard" style="color: var(--neon-blue);">BACK</a>
    </div>
    
    <div class="card">
        <h3>SYSTEM_STATUS</h3>
        <p>BOT STATE: <span style="color: var(--neon-green);">ONLINE</span></p>
        <p>DATABASE: CONNECTED</p>
        <p>RAM USAGE: 12% (NOMINAL)</p>
    </div>

    <div class="card">
        <h3>USER_MANAGEMENT</h3>
        <table>
            <tr><th>ID</th><th>CODENAME</th><th>STATUS</th><th>ACTION</th></tr>
            {% for u in users %}
            <tr>
                <td>{{ u[0] }}</td>
                <td>{{ u[1] }}</td>
                <td style="color: {{ 'var(--neon-green)' if u[2]=='active' else 'var(--neon-red)' }}">{{ u[2].upper() }}</td>
                <td>
                    {% if u[2] == 'pending' %}
                    <a href="#" style="color: var(--neon-green);">[APPROVE]</a>
                    {% endif %}
                    <a href="#" style="color: var(--neon-red);">[BAN]</a>
                </td>
            </tr>
            {% endfor %}
        </table>
    </div>
    {% endblock %}
    """, users=users)

# --- LIFECYCLE MANAGERS ---
@app.before_serving
async def startup():
    await init_db()
    asyncio.create_task(cyber_bot.start())

@app.after_serving
async def shutdown():
    if cyber_bot.client:
        await cyber_bot.client.disconnect()

# --- ENTRY POINT ---
if __name__ == "__main__":
    # Termux/Local Dev: Run directly
    # Cloud (Render): Uses Hypercorn via Procfile
    config = Config()
    config.bind = [f"0.0.0.0:{os.getenv('PORT', '8000')}"]
    asyncio.run(serve(app, config))
