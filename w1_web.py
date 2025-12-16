import os, sys, json, asyncio, logging, io, csv
from datetime import datetime
import pytz, secrets
import aiosqlite
from quart import Quart, request, redirect, session, Response, render_template_string
from telethon import TelegramClient
from telethon.tl.types import UserStatusOnline, InputPhoneContact
from telethon.tl.functions.contacts import ImportContactsRequest
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
client = None  # Start as None to prevent crash

def get_client():
    """Creates the client ONLY if config is valid."""
    global client
    if client is None:
        # If credentials are missing, we cannot create client yet
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
    # Wait until setup is actually done
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
            tg = get_client() # Re-fetch to be safe
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

# ===================== STYLES =====================
STYLE = """
<style>
body{background:#0f172a;color:white;font-family:sans-serif}
.auth{max-width:400px;margin:50px auto;background:#1e293b;padding:25px;border-radius:15px}
input,button{width:100%;padding:12px;margin:8px 0;border-radius:10px;border:none}
button{background:#3b82f6;color:white}
a{color:#93c5fd;text-decoration:none}
</style>
"""

# ===================== SETUP =====================
@app.route("/setup")
async def setup():
    if cfg["is_setup_done"]:
        return redirect("/login")
    return await render_template_string(STYLE + """
<div class=auth>
<h3>Initial Setup</h3>
<form method=post action=/do_setup>
<input name=api_id placeholder="API ID" required>
<input name=api_hash placeholder="API HASH" required>
<input name=phone placeholder="+91..." required>
<hr>
<input name=username placeholder="Admin Username" required>
<input type=password name=password placeholder="Admin Password" required>
<button>Save</button>
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
        "phone": f["phone"],
        "admin_username": f["username"],
        "admin_password": f["password"],
        "is_setup_done": True
    })
    save_config(cfg)
    
    # Initialize client now that we have credentials
    get_client()
    
    # Trigger restart to refresh background tasks safely
    if os.path.exists("session_pro.session"):
        try:
            os.remove("session_pro.session")
        except:
            pass
    os.execv(sys.executable, ["python"] + sys.argv)

# ===================== LOGIN =====================
@app.route("/login")
async def login():
    return await render_template_string(STYLE + """
<div class=auth>
<h3>Login</h3>
<form method=post action=/do_login>
<input name=username placeholder=Username required>
<input type=password name=password placeholder=Password required>
<button>Login</button>
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
    # Ensure client exists
    tg = get_client()
    if not tg:
         return "Error: Client not initialized. Complete setup first."

    if request.method == "POST":
        f = await request.form
        try:
            await tg.sign_in(phone=TG_LOGIN["phone"], code=f["code"])
            TG_LOGIN["need_code"] = False
            return redirect("/")
        except Exception as e:
            return f"OTP Error: {e}"

    return await render_template_string(STYLE + """
<div class=auth>
<h3>Telegram OTP</h3>
<form method=post>
<input name=code placeholder="12345" required>
<button>Verify</button>
</form>
<p>Check your Telegram app for the code.</p>
</div>
""")

# ===================== DASHBOARD =====================
@app.route("/")
async def home():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT * FROM targets") as c:
            rows = await c.fetchall()
    return await render_template_string(STYLE + """
<div class=auth>
<h3>Targets</h3>
{% for r in rows %}
<p>
    <b>{{r[2]}}</b> <br>
    Status: {{r[3]}} <br>
    <small>Last Seen: {{r[4]}}</small>
</p>
<hr>
{% endfor %}
<a href=/add>+ Add Target</a>
</div>
""", rows=rows)

# ===================== ADD TARGET =====================
@app.route("/add", methods=["GET","POST"])
async def add():
    if request.method == "POST":
        f = await request.form
        tg = get_client()
        if not tg:
            return "Error: Telegram client not active."
            
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
            return f"Error finding user: {e} <br> <a href='/add'>Try Again</a>"
            
    return await render_template_string(STYLE + """
<div class=auth>
<h3>Add Target</h3>
<form method=post>
<input name=target placeholder="username (e.g. @elonmusk)" required>
<button>Add</button>
</form>
<a href="/">Cancel</a>
</div>
""")

# ===================== STARTUP =====================
@app.before_serving
async def start():
    print("🔑 RECOVERY KEY:", cfg["recovery_key"])
    await init_db()
    # Only start tracker loop; it will wait inside if setup isn't done
    app.add_background_task(tracker_loop)

# ===================== RENDER SERVER =====================
import hypercorn.asyncio
from hypercorn.config import Config

if __name__ == "__main__":
    c = Config()
    c.bind = [f"0.0.0.0:{os.environ.get('PORT', 10000)}"]
    asyncio.run(hypercorn.asyncio.serve(app, c))
