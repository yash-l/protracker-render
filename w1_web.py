import os, sys, json, asyncio, logging, io, csv
from datetime import datetime
import pytz, secrets
import aiosqlite
from quart import Quart, request, redirect, session, Response, render_template_string
from telethon import TelegramClient
from telethon.tl.types import UserStatusOnline
import python_socks

# ===================== PATHS (RENDER SAFE) =====================
BASE_DIR = "/opt/data" if os.path.exists("/opt/data") else "."
DB_FILE = f"{BASE_DIR}/tracker.db"
CONFIG_FILE = f"{BASE_DIR}/config.json"
PIC_FOLDER = f"{BASE_DIR}/profile_pics"
os.makedirs(PIC_FOLDER, exist_ok=True)

# ===================== DEFAULT CONFIG =====================
DEFAULT_CONFIG = {
    "api_id": 0,
    "api_hash": "",
    "phone": "",
    "admin_username": "admin",
    "admin_password": "password",
    "timezone": "Asia/Kolkata",
    "recovery_key": secrets.token_hex(8),
    "secret_key": secrets.token_hex(16),
    "is_setup_done": False
}

# ===================== LOAD CONFIG =====================
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            c = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                c.setdefault(k, v)
            return c
    return DEFAULT_CONFIG.copy()

def save_config(c):
    with open(CONFIG_FILE, "w") as f:
        json.dump(c, f, indent=4)

cfg = load_config()

# ===================== TELEGRAM LOGIN STATE =====================
TG_LOGIN = {"need_code": False, "phone": None}

# ===================== TELETHON (LAZY INIT FIX) =====================
client = None 

def get_client():
    """Creates the client ONLY if config is valid."""
    global client
    if client is None:
        if not cfg["api_id"] or not cfg["api_hash"]:
            return None
        client = TelegramClient(
            "session_pro",
            cfg["api_id"],
            cfg["api_hash"],
            proxy=(python_socks.HTTP, "127.0.0.1", 8080, True) if False else None
        )
    return client

# ===================== QUART =====================
app = Quart(__name__)
app.secret_key = cfg["secret_key"]

# ===================== DATABASE =====================
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS targets(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            current_status TEXT,
            last_seen TEXT
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS sessions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            status TEXT,
            time TEXT
        )""")
        await db.commit()

# ===================== HELPERS =====================
def now():
    return datetime.now(pytz.timezone(cfg["timezone"])).strftime("%I:%M %p")

# ===================== TRACKER =====================
async def tracker_loop():
    while not cfg["is_setup_done"]:
        await asyncio.sleep(5)

    tg = get_client()
    if tg is None:
        print("⚠️ Waiting for client initialization...")
        return

    try:
        if not tg.is_connected():
            await tg.start(phone=cfg["phone"])
    except Exception as e:
        if "code" in str(e).lower() or "auth" in str(e).lower():
            TG_LOGIN["need_code"] = True
            TG_LOGIN["phone"] = cfg["phone"]
            print("📲 Telegram OTP required → /telegram-login")
            return
        print(f"Tracker Error: {e}")
        return

    memory = {}
    while True:
        try:
            tg = get_client()
            if not tg or not tg.is_connected():
                await asyncio.sleep(5)
                continue

            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute("SELECT user_id FROM targets") as c:
                    users = await c.fetchall()

            for (uid,) in users:
                try:
                    u = await tg.get_entity(uid)
                    status = "online" if isinstance(u.status, UserStatusOnline) else "offline"
                    
                    if memory.get(uid) != status:
                        async with aiosqlite.connect(DB_FILE) as db:
                            await db.execute(
                                "INSERT INTO sessions (user_id,status,time) VALUES (?,?,?)",
                                (uid, status.upper(), now())
                            )
                            await db.execute(
                                "UPDATE targets SET current_status=?, last_seen=? WHERE user_id=?",
                                (status, now(), uid)
                            )
                            await db.commit()
                    memory[uid] = status
                except Exception as e:
                    print(f"Error checking {uid}: {e}")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"Loop Error: {e}")
            await asyncio.sleep(5)

# ===================== AUTH GUARD =====================
@app.before_request
def guard():
    if request.path.startswith("/static"):
        return
    if request.path in ("/setup", "/do_setup", "/login", "/do_login", "/telegram-login"):
        return
    if not cfg["is_setup_done"]:
        return redirect("/setup")
    if "user" not in session:
        return redirect("/login")

# ===================== ADVANCED UI/UX STYLES =====================
# This block defines the modern "Glassmorphism" look
STYLE = """
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<style>
:root {
    --bg-color: #0f172a;
    --card-bg: rgba(30, 41, 59, 0.7);
    --primary: #3b82f6;
    --primary-hover: #2563eb;
    --text-main: #f1f5f9;
    --text-sub: #94a3b8;
    --border: rgba(255, 255, 255, 0.1);
    --green-glow: #22c55e;
    --red-dim: #ef4444;
}

body {
    background: radial-gradient(circle at top, #1e293b, #0f172a);
    color: var(--text-main);
    font-family: system-ui, -apple-system, sans-serif;
    margin: 0;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
}

/* Animations */
@keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
@keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.4); } 70% { box-shadow: 0 0 0 10px rgba(34, 197, 94, 0); } 100% { box-shadow: 0 0 0 0 rgba(34, 197, 94, 0); } }

/* Glass Card */
.glass-container {
    background: var(--card-bg);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 2rem;
    width: 90%;
    max-width: 420px;
    box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.3);
    animation: fadeIn 0.5s ease-out;
}

.dashboard-container {
    width: 95%;
    max-width: 600px;
    margin-top: 20px;
    padding-bottom: 80px; /* Space for FAB */
    align-self: center;
    justify-content: flex-start;
}

/* Typography */
h3 { margin: 0 0 1.5rem 0; font-size: 1.5rem; font-weight: 700; text-align: center; letter-spacing: -0.025em; }
p { color: var(--text-sub); margin: 0.5rem 0; }
small { font-size: 0.85rem; color: var(--text-sub); }

/* Forms */
input {
    width: 100%;
    box-sizing: border-box;
    padding: 14px;
    margin-bottom: 12px;
    background: rgba(15, 23, 42, 0.6);
    border: 1px solid var(--border);
    border-radius: 12px;
    color: white;
    font-size: 1rem;
    transition: all 0.2s;
}
input:focus { outline: none; border-color: var(--primary); background: rgba(15, 23, 42, 0.9); }

/* Buttons */
button {
    width: 100%;
    padding: 14px;
    background: var(--primary);
    color: white;
    border: none;
    border-radius: 12px;
    font-weight: 600;
    font-size: 1rem;
    cursor: pointer;
    transition: background 0.2s;
    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2);
}
button:hover { background: var(--primary-hover); }

/* Dashboard Items */
.target-card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 16px;
    margin-bottom: 12px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    transition: transform 0.2s;
}
.target-card:active { transform: scale(0.98); }

.user-info { display: flex; flex-direction: column; }
.username { font-weight: 600; font-size: 1.1rem; }
.status-badge { 
    padding: 6px 12px; 
    border-radius: 20px; 
    font-size: 0.8rem; 
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
}

.status-online {
    background: rgba(34, 197, 94, 0.2);
    color: #4ade80;
    border: 1px solid rgba(34, 197, 94, 0.3);
    display: flex;
    align-items: center;
    gap: 6px;
}
.dot {
    width: 8px; 
    height: 8px; 
    background: #4ade80; 
    border-radius: 50%; 
    animation: pulse 2s infinite;
}

.status-offline {
    background: rgba(148, 163, 184, 0.1);
    color: var(--text-sub);
}

/* Floating Action Button (FAB) */
.fab {
    position: fixed;
    bottom: 24px;
    right: 24px;
    background: var(--primary);
    width: 56px;
    height: 56px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    color: white;
    font-size: 24px;
    text-decoration: none;
    box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.3);
    transition: transform 0.2s;
}
.fab:hover { transform: scale(1.1); }

.link-btn { text-align: center; display: block; margin-top: 15px; color: var(--primary); }
</style>
"""

# ===================== SETUP =====================
@app.route("/setup")
async def setup():
    if cfg["is_setup_done"]:
        return redirect("/login")
    return await render_template_string(STYLE + """
<div class="glass-container">
    <h3>🚀 Tracker Setup</h3>
    <form method=post action=/do_setup>
        <p>Telegram API Details</p>
        <input name=api_id placeholder="API ID (e.g. 12345)" type="number" required>
        <input name=api_hash placeholder="API Hash" required>
        <input name=phone placeholder="Your Phone (+91...)" required>
        
        <p style="margin-top:20px">Admin Security</p>
        <input name=username placeholder="Create Username" required>
        <input type=password name=password placeholder="Create Password" required>
        
        <button style="margin-top:10px">Initialize System</button>
    </form>
</div>
""")

@app.route("/do_setup", methods=["POST"])
async def do_setup():
    # FIX: No Restart Loop here. Updates config in memory and starts client.
    global cfg
    f = await request.form
    cfg.update({
        "api_id": int(f["api_id"]),
        "api_hash": f["api_hash"],
        "phone": f["phone"],
        "admin_username": f["username"],
        "admin_password": f["password"],
        "is_setup_done": True
    })
    save_config(cfg)
    get_client() # Lazy load init
    return redirect("/login")

# ===================== LOGIN =====================
@app.route("/login")
async def login():
    return await render_template_string(STYLE + """
<div class="glass-container">
    <h3>🔐 Admin Access</h3>
    <form method=post action=/do_login>
        <input name=username placeholder="Username" required>
        <input type=password name=password placeholder="Password" required>
        <button>Enter Dashboard</button>
    </form>
</div>
""")

@app.route("/do_login", methods=["POST"])
async def do_login():
    f = await request.form
    if f["username"] == cfg["admin_username"] and f["password"] == cfg["admin_password"]:
        session["user"] = f["username"]
        return redirect("/")
    return redirect("/login")

# ===================== TELEGRAM OTP =====================
@app.route("/telegram-login", methods=["GET", "POST"])
async def telegram_login():
    tg = get_client()
    if not tg: return "Error: System not initialized."

    if request.method == "POST":
        f = await request.form
        try:
            await tg.sign_in(phone=TG_LOGIN["phone"], code=f["code"])
            TG_LOGIN["need_code"] = False
            return redirect("/")
        except Exception as e:
            return f"<div class='glass-container'><h3>❌ Error</h3><p>{e}</p><a href='/telegram-login' class='link-btn'>Try Again</a></div>"

    return await render_template_string(STYLE + """
<div class="glass-container">
    <h3>📲 Verify Telegram</h3>
    <p>We sent a code to your Telegram app.</p>
    <form method=post>
        <input name=code placeholder="Enter OTP Code" required type="number">
        <button>Verify & Start</button>
    </form>
</div>
""")

# ===================== DASHBOARD =====================
@app.route("/")
async def home():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT * FROM targets") as c:
            rows = await c.fetchall()
            
    # Logic to format rows into cards
    cards_html = ""
    for r in rows:
        uid, username, target_input, status, last_seen = r
        
        status_class = "status-online" if status == "online" else "status-offline"
        dot_html = "<div class='dot'></div>" if status == "online" else ""
        
        cards_html += f"""
        <div class="target-card">
            <div class="user-info">
                <div class="username">{username if username else target_input}</div>
                <small>Last Seen: {last_seen}</small>
            </div>
            <div class="status-badge {status_class}">
                {dot_html}
                {status.upper()}
            </div>
        </div>
        """

    if not rows:
        cards_html = "<p style='text-align:center; padding:20px;'>No targets being tracked yet.</p>"

    return await render_template_string(STYLE + f"""
<div class="dashboard-container" style="display:block">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px;">
        <h3>📡 Live Tracker</h3>
        <small style="color:var(--primary)">RUNNING</small>
    </div>
    
    {cards_html}

    <a href="/add" class="fab">+</a>
</div>
""")

# ===================== ADD TARGET =====================
@app.route("/add", methods=["GET","POST"])
async def add():
    if request.method == "POST":
        f = await request.form
        tg = get_client()
        if not tg: return "Error: Client down."
        try:
            e = await tg.get_entity(f["target"])
            async with aiosqlite.connect(DB_FILE) as db:
                await db.execute(
                    "INSERT OR IGNORE INTO targets VALUES (?,?,?,?,?)",
                    (e.id, e.username or "", f["target"], "checking", "new")
                )
                await db.commit()
            return redirect("/")
        except Exception as e:
             return await render_template_string(STYLE + f"<div class='glass-container'><h3>❌ Failed</h3><p>{e}</p><a href='/add' class='link-btn'>Try Again</a></div>")
            
    return await render_template_string(STYLE + """
<div class="glass-container">
    <h3>🎯 Add Target</h3>
    <form method=post>
        <input name=target placeholder="Username (e.g. @elonmusk)" required>
        <button>Start Tracking</button>
    </form>
    <a href="/" class="link-btn">Cancel</a>
</div>
""")

# ===================== STARTUP =====================
@app.before_serving
async def start():
    print("🔑 RECOVERY KEY:", cfg["recovery_key"])
    await init_db()
    app.add_background_task(tracker_loop)

# ===================== RENDER SERVER =====================
import hypercorn.asyncio
from hypercorn.config import Config

if __name__ == "__main__":
    c = Config()
    c.bind = [f"0.0.0.0:{os.environ.get('PORT', 10000)}"]
    asyncio.run(hypercorn.asyncio.serve(app, c))
