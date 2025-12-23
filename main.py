import asyncio
import logging
import os
import sys
import secrets
import pytz
from datetime import datetime, timedelta
from functools import wraps

import aiosqlite
from quart import Quart, render_template_string, request, redirect, url_for, session, abort, jsonify
from hypercorn.config import Config
from hypercorn.asyncio import serve

from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.types import UserStatusOnline, UserStatusOffline, UserStatusRecently

# --- CONFIGURATION ---
# Use values from your uploaded file as defaults, but allow Env Vars override
API_ID = int(os.getenv("API_ID", "9497762")) 
API_HASH = os.getenv("API_HASH", "272c77bf080e4a82846b8ff3dc3df0f4")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
TIMEZONE = 'Asia/Kolkata'

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("Netrunner")

# --- APP SETUP ---
app = Quart(__name__)
app.secret_key = SECRET_KEY

# Global variables for login flow
temp_client = None 
phone_number = None
phone_code_hash = None

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
    display_name TEXT,
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

# --- UI TEMPLATE (Cyberpunk Style) ---
HTML_BASE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NETRUNNER // SYSTEM</title>
    <style>
        :root { --bg: #050505; --panel: #111; --green: #0f0; --red: #f00; --blue: #00f3ff; }
        body { background: var(--bg); color: #ddd; font-family: monospace; margin: 0; padding: 20px; }
        .card { background: var(--panel); border: 1px solid #333; padding: 20px; margin-bottom: 20px; max-width: 600px; margin: 20px auto; }
        h2 { color: var(--green); border-bottom: 1px solid var(--green); padding-bottom: 10px; }
        input, button { width: 100%; padding: 10px; margin-top: 10px; background: #000; border: 1px solid #444; color: var(--blue); }
        button { background: var(--green); color: #000; font-weight: bold; cursor: pointer; }
        button:hover { background: #fff; }
        .status-online { color: var(--green); font-weight: bold; }
        .status-offline { color: var(--red); }
        .alert { padding: 10px; background: rgba(255,0,0,0.2); border: 1px solid var(--red); color: var(--red); text-align: center; }
    </style>
</head>
<body>
    <div style="text-align:center; color:var(--green); font-size:1.5rem;">NETRUNNER // V4</div>
    {{ CONTENT }}
</body>
</html>
"""

# --- DB HELPERS ---
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
        # Create Admin
        async with db.execute("SELECT * FROM users WHERE username = ?", (ADMIN_USER,)) as c:
            if not await c.fetchone():
                await db.execute("INSERT INTO users (username, password, is_admin) VALUES (?, ?, 1)", (ADMIN_USER, ADMIN_PASS))
                await db.commit()

async def get_session_string():
    async with await get_db() as db:
        async with db.execute("SELECT value FROM settings WHERE key='session_string'") as c:
            row = await c.fetchone()
            return row['value'] if row else None

async def save_session_string(session_str):
    async with await get_db() as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('session_string', ?)", (session_str,))
        await db.commit()

# --- HELPER: TIME CONVERSION (From your w1 file) ---
def convert_to_kolkata_time(time_obj):
    try:
        if time_obj is None: return "Unknown"
        # Ensure time_obj is timezone-aware
        if time_obj.tzinfo is None:
            time_obj = pytz.utc.localize(time_obj)
        local_tz = pytz.timezone(TIMEZONE)
        local_time = time_obj.astimezone(local_tz)
        return local_time.strftime('%I:%M %p')
    except Exception as e:
        logger.error(f"Time conversion error: {e}")
        return str(time_obj)

# --- TRACKER LOGIC (Integrated from w1.py) ---
class CyberTracker:
    def __init__(self):
        self.client = None
        self.tracking_active = False
        self.was_online = {} # Map target_id -> bool

    async def start(self):
        session_str = await get_session_string()
        if not session_str:
            logger.warning("No Session String found. Login via Web UI required.")
            return

        try:
            self.client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
            await self.client.connect()
            
            if not await self.client.is_user_authorized():
                logger.error("Session invalid. Re-login required.")
                return

            logger.info(f"Bot Connected as: {await self.client.get_me()}")
            self.tracking_active = True
            asyncio.create_task(self.loop())
        except Exception as e:
            logger.error(f"Bot Start Error: {e}")

    async def loop(self):
        while self.tracking_active:
            try:
                async with await get_db() as db:
                    async with db.execute("SELECT * FROM targets WHERE is_tracking = 1") as c:
                        targets = await c.fetchall()

                    for target in targets:
                        await self.check_target(db, target)
                        
            except Exception as e:
                logger.error(f"Loop Error: {e}")
            
            await asyncio.sleep(3) # Your requested 3-second interval

    async def check_target(self, db, target):
        t_id = target['id']
        tg_id = target['tg_id']
        last_status = target['last_status']
        
        try:
            entity = await self.client.get_entity(tg_id)
            status_obj = entity.status
            
            # Determine status based on your script's logic
            current_status = 'unknown'
            if isinstance(status_obj, UserStatusOnline):
                current_status = 'online'
            elif isinstance(status_obj, UserStatusOffline):
                current_status = 'offline'
            elif isinstance(status_obj, UserStatusRecently):
                current_status = 'recently'

            # Logic: Detect Change
            if current_status != last_status:
                now = datetime.now(pytz.timezone(TIMEZONE))
                
                # Update DB
                await db.execute("UPDATE targets SET last_status = ?, last_seen = ? WHERE id = ?", (current_status, now, t_id))
                await db.execute("INSERT INTO logs (target_id, event_type, timestamp) VALUES (?, ?, ?)", (t_id, current_status.upper(), now))
                await db.commit()
                
                # "Send Message" Logic (Simulated by logging for now, or you can add bot API call here)
                msg = ""
                if current_status == 'online':
                    msg = f"Target {target['display_name']} went ONLINE at {convert_to_kolkata_time(now)}"
                elif current_status == 'offline':
                    msg = f"Target {target['display_name']} went OFFLINE at {convert_to_kolkata_time(now)}"
                
                if msg: logger.info(f"NOTIFICATION: {msg}")

        except Exception as e:
            # logger.error(f"Error checking {tg_id}: {e}")
            pass

cyber_bot = CyberTracker()

# --- WEB ROUTES ---
def login_required(f):
    @wraps(f)
    async def decorated(*args, **kwargs):
        if 'user_id' not in session: return redirect('/login')
        return await f(*args, **kwargs)
    return decorated

@app.route('/')
async def index():
    return redirect('/dashboard')

@app.route('/login', methods=['GET', 'POST'])
async def login():
    if request.method == 'POST':
        form = await request.form
        async with await get_db() as db:
            async with db.execute("SELECT * FROM users WHERE username = ? AND password = ?", (form.get('username'), form.get('password'))) as c:
                user = await c.fetchone()
                if user:
                    session['user_id'] = user['id']
                    return redirect('/dashboard')
    
    content = """
    <div class="card">
        <h2>LOGIN</h2>
        <form method="POST">
            <input type="text" name="username" placeholder="Username" required>
            <input type="password" name="password" placeholder="Password" required>
            <button type="submit">ENTER SYSTEM</button>
        </form>
    </div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

# --- DASHBOARD & TELEGRAM CONNECT ---
@app.route('/dashboard')
@login_required
async def dashboard():
    # Check if bot is connected
    is_connected = cyber_bot.tracking_active
    
    # Fetch Targets
    async with await get_db() as db:
        async with db.execute("SELECT * FROM targets") as c:
            targets = await c.fetchall()

    target_rows = ""
    for t in targets:
        color = "status-online" if t['last_status'] == 'online' else "status-offline"
        target_rows += f"""
        <div style="border-top:1px solid #333; padding:10px;">
            <strong style="font-size:1.2em;">{t['display_name']}</strong> <br>
            ID: {t['tg_id']} <br>
            STATUS: <span class="{color}">{t['last_status'].upper()}</span> <br>
            LAST SEEN: {convert_to_kolkata_time(t['last_seen']) if t['last_seen'] else 'Never'}
        </div>
        """

    connect_btn = ""
    if not is_connected:
        connect_btn = """
        <div class="alert">⚠️ TELEGRAM NOT CONNECTED</div>
        <a href="/connect_telegram"><button>CONNECT TELEGRAM ACCOUNT</button></a>
        """
    else:
        connect_btn = '<div style="color:var(--green); text-align:center; padding:10px;">✅ SYSTEM OPERATIONAL</div>'

    content = f"""
    <div class="card">
        <h2>SYSTEM STATUS</h2>
        {connect_btn}
    </div>
    <div class="card">
        <h2>TARGETS <a href="/add" style="float:right; font-size:0.6em;">[+ ADD]</a></h2>
        {target_rows}
    </div>
    <div style="text-align:center;"><a href="/logout">LOGOUT</a></div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

# --- WEB-BASED TELEGRAM LOGIN FLOW ---
@app.route('/connect_telegram', methods=['GET', 'POST'])
@login_required
async def connect_telegram():
    global temp_client, phone_number, phone_code_hash
    
    msg = ""
    if request.method == 'POST':
        form = await request.form
        phone = form.get('phone')
        
        try:
            # Initialize Client for login
            temp_client = TelegramClient(StringSession(), API_ID, API_HASH)
            await temp_client.connect()
            
            if not await temp_client.is_user_authorized():
                send_code = await temp_client.send_code_request(phone)
                phone_number = phone
                phone_code_hash = send_code.phone_code_hash
                return redirect('/verify_otp')
            else:
                msg = "Already authorized!"
        except Exception as e:
            msg = f"Error: {str(e)}"

    content = f"""
    <div class="card">
        <h2>CONNECT TELEGRAM</h2>
        <p style="color:#888;">Enter your phone number (with country code) to receive OTP.</p>
        <p style="color:red;">{msg}</p>
        <form method="POST">
            <input type="text" name="phone" placeholder="+918849404331" required value="+91">
            <button type="submit">SEND OTP</button>
        </form>
        <br><a href="/dashboard">Back</a>
    </div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/verify_otp', methods=['GET', 'POST'])
@login_required
async def verify_otp():
    global temp_client, phone_number, phone_code_hash
    
    msg = ""
    if request.method == 'POST':
        code = (await request.form).get('code')
        try:
            await temp_client.sign_in(phone=phone_number, code=code, phone_code_hash=phone_code_hash)
            
            # Save Session
            session_string = temp_client.session.save()
            await save_session_string(session_string)
            await temp_client.disconnect()
            
            # Start the main bot
            asyncio.create_task(cyber_bot.start())
            
            return redirect('/dashboard')
        except errors.SessionPasswordNeededError:
            return redirect('/verify_2fa')
        except Exception as e:
            msg = f"Error: {e}"

    content = f"""
    <div class="card">
        <h2>ENTER OTP</h2>
        <p style="color:#888;">Check your Telegram App for the code.</p>
        <p style="color:red;">{msg}</p>
        <form method="POST">
            <input type="text" name="code" placeholder="12345" required>
            <button type="submit">VERIFY & CONNECT</button>
        </form>
    </div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/verify_2fa', methods=['GET', 'POST'])
@login_required
async def verify_2fa():
    global temp_client
    msg = ""
    if request.method == 'POST':
        password = (await request.form).get('password')
        try:
            await temp_client.sign_in(password=password)
            session_string = temp_client.session.save()
            await save_session_string(session_string)
            await temp_client.disconnect()
            asyncio.create_task(cyber_bot.start())
            return redirect('/dashboard')
        except Exception as e:
            msg = f"Error: {e}"

    content = f"""
    <div class="card">
        <h2>TWO-STEP VERIFICATION</h2>
        <p>Your account is protected by a password.</p>
        <p style="color:red;">{msg}</p>
        <form method="POST">
            <input type="password" name="password" placeholder="Your Cloud Password" required>
            <button type="submit">UNLOCK</button>
        </form>
    </div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/add', methods=['GET', 'POST'])
@login_required
async def add_target():
    if request.method == 'POST':
        form = await request.form
        async with await get_db() as db:
            await db.execute("INSERT INTO targets (tg_id, display_name, last_status) VALUES (?, ?, 'unknown')", 
                             (form.get('tg_id'), form.get('name')))
            await db.commit()
        return redirect('/dashboard')

    content = """
    <div class="card">
        <h2>ADD TARGET</h2>
        <form method="POST">
            <input type="text" name="name" placeholder="Name" required>
            <input type="number" name="tg_id" placeholder="Telegram ID (Numeric)" required>
            <button type="submit">START TRACKING</button>
        </form>
    </div>
    """
    return await render_template_string(HTML_BASE.replace('{{ CONTENT }}', content))

@app.route('/logout')
async def logout():
    session.clear()
    return redirect('/login')

# --- LIFECYCLE ---
@app.before_serving
async def startup():
    await init_db()
    # Try to start bot if session exists
    asyncio.create_task(cyber_bot.start())

if __name__ == "__main__":
    config = Config()
    config.bind = [f"0.0.0.0:{os.getenv('PORT', '8000')}"]
    asyncio.run(serve(app, config))
