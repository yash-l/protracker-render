import os, sys, json, asyncio, logging, io, csv
from datetime import datetime
import pytz, secrets
import aiosqlite
from quart import Quart, request, redirect, session, Response, render_template_string
from telethon import TelegramClient, errors
from telethon.tl.types import UserStatusOnline
import python_socks

# ===================== PATHS =====================
BASE_DIR = "/opt/data" if os.path.exists("/opt/data") else "."
DB_FILE = f"{BASE_DIR}/tracker.db"
CONFIG_FILE = f"{BASE_DIR}/config.json"
SESSION_FILE = f"{BASE_DIR}/session_pro"
PIC_FOLDER = f"{BASE_DIR}/profile_pics"
os.makedirs(PIC_FOLDER, exist_ok=True)

# ===================== DEFAULT CONFIG =====================
DEFAULT_CONFIG = {
    "api_id": 0,
    "api_hash": "",
    "phone": "",  # Will be set in the new phone screen
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

# ===================== TELETHON CLIENT =====================
client = None 

def get_client():
    global client
    if client is None:
        if not cfg["api_id"] or not cfg["api_hash"]:
            return None
        client = TelegramClient(
            SESSION_FILE,
            cfg["api_id"],
            cfg["api_hash"],
            proxy=(python_socks.HTTP, "127.0.0.1", 8080, True) if False else None
        )
    return client

# ===================== QUART APP =====================
app = Quart(__name__)
app.secret_key = cfg["secret_key"]

# ===================== DATABASE =====================
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS targets(user_id INTEGER PRIMARY KEY, username TEXT, display_name TEXT, current_status TEXT, last_seen TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, status TEXT, time TEXT)")
        await db.commit()

# ===================== TRACKER LOOP =====================
async def tracker_loop():
    while True:
        try:
            tg = get_client()
            # If not connected or not authorized, wait.
            # We don't force login here anymore; the UI flow handles it.
            if not tg or not tg.is_connected() or not await tg.is_user_authorized():
                await asyncio.sleep(5)
                continue

            # Tracking Logic
            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute("SELECT user_id FROM targets") as c:
                    users = await c.fetchall()

            memory = {}
            for (uid,) in users:
                try:
                    u = await tg.get_entity(uid)
                    status = "online" if isinstance(u.status, UserStatusOnline) else "offline"
                    # Simple log logic
                    now_str = datetime.now(pytz.timezone(cfg["timezone"])).strftime("%I:%M %p")
                    
                    async with aiosqlite.connect(DB_FILE) as db:
                        # Update Last Seen
                        await db.execute("UPDATE targets SET current_status=?, last_seen=? WHERE user_id=?", (status, now_str, uid))
                        
                        # Log session if status changed
                        # (In a real scenario, you'd check previous status from DB or memory)
                        if status == "online":
                            # check if last entry was online to avoid duplicate spam
                            async with db.execute("SELECT status FROM sessions WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)) as cur:
                                last = await cur.fetchone()
                            if not last or last[0] != "ONLINE":
                                await db.execute("INSERT INTO sessions (user_id,status,time) VALUES (?,?,?)", (uid, "ONLINE", now_str))
                        await db.commit()
                except:
                    pass
            await asyncio.sleep(5)
        except:
            await asyncio.sleep(5)

# ===================== AUTH GUARD =====================
@app.before_request
async def guard():
    if request.path.startswith("/static"): return
    # Public routes
    if request.path in ("/setup", "/do_setup", "/login", "/do_login", "/reset", "/do_reset"):
        return

    # 1. Setup Check
    if not cfg["is_setup_done"]:
        return redirect("/setup")

    # 2. Admin Login Check
    if "user" not in session:
        return redirect("/login")

    # 3. Telegram Authorization Check
    # If we are already on phone/OTP pages, let them pass
    if request.path in ("/enter-phone", "/send-code", "/telegram-login", "/verify-code"):
        return

    # For Dashboard or any other page, CHECK if Telegram is connected
    tg = get_client()
    if not tg:
        return redirect("/setup") # Should not happen if setup is done
    
    # Connect if needed
    if not tg.is_connected():
        try:
            await tg.connect()
        except:
            pass
    
    # CRITICAL: If not authorized, FORCE user to Enter Phone Page
    if not await tg.is_user_authorized():
        return redirect("/enter-phone")

# ===================== UI STYLES =====================
STYLE = """
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<style>
:root { --bg: #0f172a; --card: rgba(30, 41, 59, 0.7); --primary: #3b82f6; --text: #f1f5f9; }
body { background: radial-gradient(circle at top, #1e293b, #0f172a); color: var(--text); font-family: sans-serif; margin: 0; min-height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; }
.glass-container { background: var(--card); backdrop-filter: blur(12px); border: 1px solid rgba(255,255,255,0.1); border-radius: 20px; padding: 2rem; width: 90%; max-width: 420px; box-shadow: 0 20px 25px rgba(0,0,0,0.3); animation: fadeIn 0.5s; }
input { width: 100%; padding: 14px; margin-bottom: 12px; background: rgba(15,23,42,0.6); border: 1px solid rgba(255,255,255,0.1); border-radius: 12px; color: white; box-sizing: border-box; font-size: 16px; }
button { width: 100%; padding: 14px; background: var(--primary); color: white; border: none; border-radius: 12px; font-weight: bold; cursor: pointer; font-size: 16px; }
h3 { margin-top: 0; text-align: center; }
@keyframes fadeIn { from { opacity:0; transform:translateY(10px); } to { opacity:1; transform:translateY(0); } }
</style>
"""

# ===================== 1. SETUP PAGE =====================
@app.route("/setup")
async def setup():
    if cfg["is_setup_done"]: return redirect("/login")
    return await render_template_string(STYLE + """
<div class="glass-container">
    <h3>🛠 System Setup</h3>
    <form method=post action=/do_setup>
        <p>Step 1: Telegram API</p>
        <input name=api_id placeholder="API ID" type="number" required>
        <input name=api_hash placeholder="API Hash" required>
        <hr style="border-color:#ffffff20">
        <p>Step 2: Admin Security</p>
        <input name=username placeholder="Create Username" required>
        <input type=password name=password placeholder="Create Password" required>
        <button>Save & Continue</button>
    </form>
</div>
""")

@app.route("/do_setup", methods=["POST"])
async def do_setup():
    global cfg
    f = await request.form
    cfg.update({
        "api_id": int(f["api_id"]),
        "api_hash": f["api_hash"],
        "admin_username": f["username"],
        "admin_password": f["password"],
        "is_setup_done": True
    })
    save_config(cfg)
    get_client()
    return redirect("/login")

# ===================== 2. LOGIN PAGE =====================
@app.route("/login")
async def login():
    return await render_template_string(STYLE + """
<div class="glass-container">
    <h3>🔐 Admin Login</h3>
    <form method=post action=/do_login>
        <input name=username placeholder="Username" required>
        <input type=password name=password placeholder="Password" required>
        <button>Login</button>
    </form>
    <br><a href="/reset" style="color:#ef4444; text-decoration:none; font-size:12px; display:block; text-align:center">Reset System</a>
</div>
""")

@app.route("/do_login", methods=["POST"])
async def do_login():
    f = await request.form
    if f["username"] == cfg["admin_username"] and f["password"] == cfg["admin_password"]:
        session["user"] = f["username"]
        # Redirect will be handled by guard() -> if not authorized, it goes to /enter-phone
        return redirect("/")
    return redirect("/login")

# ===================== 3. ENTER PHONE (NEW FRAME) =====================
@app.route("/enter-phone")
async def enter_phone_page():
    return await render_template_string(STYLE + """
<div class="glass-container">
    <h3>📱 Connect Telegram</h3>
    <p style="color:#94a3b8; text-align:center">Enter your mobile number to start the tracker.</p>
    <form method=post action=/send-code>
        <input name=phone placeholder="+919876543210" required>
        <button>Send OTP</button>
    </form>
</div>
""")

@app.route("/send-code", methods=["POST"])
async def send_code():
    global cfg
    f = await request.form
    phone = f["phone"].strip()
    
    # Save phone to config
    cfg["phone"] = phone
    save_config(cfg)
    
    tg = get_client()
    if not tg: return "Error: API ID missing. Reset app."
    
    try:
        if not tg.is_connected(): await tg.connect()
        await tg.send_code_request(phone)
        return redirect("/telegram-login")
    except Exception as e:
        return await render_template_string(STYLE + f"<div class='glass-container'><h3>❌ Error</h3><p>{e}</p><a href='/enter-phone' style='color:#3b82f6'>Try Again</a></div>")

# ===================== 4. ENTER OTP =====================
@app.route("/telegram-login")
async def telegram_login_page():
    return await render_template_string(STYLE + f"""
<div class="glass-container">
    <h3>💬 Verify OTP</h3>
    <p style="color:#94a3b8; text-align:center">Code sent to <b>{cfg.get('phone')}</b></p>
    <form method=post action=/verify-code>
        <input name=code placeholder="12345" type="number" required>
        <button>Start Tracker</button>
    </form>
    <a href="/enter-phone" style="display:block; text-align:center; margin-top:15px; color:#3b82f6; text-decoration:none">Change Number</a>
</div>
""")

@app.route("/verify-code", methods=["POST"])
async def verify_code():
    f = await request.form
    code = f["code"]
    tg = get_client()
    
    try:
        if not tg.is_connected(): await tg.connect()
        await tg.sign_in(phone=cfg["phone"], code=code)
        return redirect("/")
    except Exception as e:
         return await render_template_string(STYLE + f"<div class='glass-container'><h3>❌ Invalid Code</h3><p>{e}</p><a href='/telegram-login' style='color:#3b82f6'>Try Again</a></div>")

# ===================== 5. DASHBOARD =====================
@app.route("/")
async def home():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT * FROM targets") as c:
            rows = await c.fetchall()

    cards = ""
    for r in rows:
        status_text = r[3].upper()
        status_color = "#4ade80" if "ONLINE" in status_text else "#64748b"
        glow = "box-shadow: 0 0 10px #4ade80;" if "ONLINE" in status_text else ""
        
        cards += f"""
        <div style="background:rgba(30,41,59,0.7); border:1px solid rgba(255,255,255,0.1); border-radius:16px; padding:16px; margin-bottom:12px; display:flex; justify-content:space-between; align-items:center; {glow}">
            <div>
                <div style="font-weight:bold; font-size:1.1rem">{r[1] or r[2]}</div>
                <small style="color:#94a3b8">Last seen: {r[4]}</small>
            </div>
            <div style="color:{status_color}; font-weight:bold; font-size:0.8rem; background:rgba(0,0,0,0.2); padding:4px 8px; border-radius:8px">{status_text}</div>
        </div>
        """
    
    if not rows: cards = "<p style='text-align:center; color:#94a3b8'>No targets added.</p>"

    return await render_template_string(STYLE + f"""
<div style="width:95%; max-width:600px; padding-bottom:80px">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px">
        <h3>📡 Active Targets</h3>
        <span style="background:#22c55e; width:10px; height:10px; border-radius:50%; box-shadow:0 0 10px #22c55e"></span>
    </div>
    {cards}
    <a href="/add" style="position:fixed; bottom:24px; right:24px; background:#3b82f6; width:56px; height:56px; border-radius:50%; display:flex; align-items:center; justify-content:center; color:white; text-decoration:none; font-size:28px; box-shadow:0 10px 20px rgba(0,0,0,0.4)">+</a>
</div>
""")

# ===================== ADD TARGET =====================
@app.route("/add", methods=["GET","POST"])
async def add():
    if request.method == "POST":
        f = await request.form
        tg = get_client()
        try:
            e = await tg.get_entity(f["target"])
            async with aiosqlite.connect(DB_FILE) as db:
                await db.execute("INSERT OR IGNORE INTO targets VALUES (?,?,?,?,?)", (e.id, e.username or "", f["target"], "CHECKING...", "Just now"))
                await db.commit()
            return redirect("/")
        except Exception as e:
            return await render_template_string(STYLE + f"<div class='glass-container'><h3>❌ User Not Found</h3><p>{e}</p><a href='/add' style='color:#3b82f6'>Try Again</a></div>")
            
    return await render_template_string(STYLE + """
<div class="glass-container">
    <h3>🎯 Add New Target</h3>
    <form method=post>
        <input name=target placeholder="Username (e.g. @elonmusk)" required>
        <button>Start Tracking</button>
    </form>
    <a href="/" style="display:block; text-align:center; margin-top:15px; color:#94a3b8; text-decoration:none">Cancel</a>
</div>
""")

# ===================== RESET =====================
@app.route("/reset")
async def reset():
    return await render_template_string(STYLE + """
<div class="glass-container">
    <h3>⚠️ Factory Reset</h3>
    <p>This will delete all settings and login data.</p>
    <form action="/do_reset" method="post">
        <button style="background:#ef4444">Confirm Reset</button>
    </form>
    <a href="/login" style="display:block; text-align:center; margin-top:15px; color:#94a3b8">Cancel</a>
</div>
""")

@app.route("/do_reset", methods=["POST"])
async def do_reset():
    if os.path.exists(CONFIG_FILE): os.remove(CONFIG_FILE)
    if os.path.exists(SESSION_FILE + ".session"): os.remove(SESSION_FILE + ".session")
    global cfg
    cfg = DEFAULT_CONFIG.copy()
    os.execv(sys.executable, ["python"] + sys.argv)

# ===================== STARTUP =====================
@app.before_serving
async def start():
    await init_db()
    app.add_background_task(tracker_loop)

if __name__ == "__main__":
    from hypercorn.config import Config
    import hypercorn.asyncio
    c = Config()
    c.bind = [f"0.0.0.0:{os.environ.get('PORT', 10000)}"]
    asyncio.run(hypercorn.asyncio.serve(app, c))
