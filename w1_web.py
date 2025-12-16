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

# ===================== TELETHON =====================
client = TelegramClient(
    "session_pro",
    cfg["api_id"],
    cfg["api_hash"],
    proxy=(python_socks.HTTP, "127.0.0.1", 8080, True) if False else None
)

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
    if not cfg["is_setup_done"]:
        return

    try:
        await client.start(phone=cfg["phone"])
    except Exception as e:
        if "code" in str(e).lower():
            TG_LOGIN["need_code"] = True
            TG_LOGIN["phone"] = cfg["phone"]
            print("📲 Telegram OTP required → /telegram-login")
            return
        raise

    memory = {}
    while True:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT user_id FROM targets") as c:
                users = await c.fetchall()

        for (uid,) in users:
            try:
                u = await client.get_entity(uid)
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
            except:
                pass
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
    return render_template_string(STYLE + """
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
    if os.path.exists("session_pro.session"):
        os.remove("session_pro.session")
    os.execv(sys.executable, ["python"] + sys.argv)

# ===================== LOGIN =====================
@app.route("/login")
async def login():
    return render_template_string(STYLE + """
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
    if not TG_LOGIN["need_code"]:
        return redirect("/")
    if request.method == "POST":
        f = await request.form
        try:
            await client.sign_in(phone=TG_LOGIN["phone"], code=f["code"])
            TG_LOGIN["need_code"] = False
            app.add_background_task(tracker_loop)
            return redirect("/")
        except Exception as e:
            return f"OTP Error: {e}"

    return render_template_string(STYLE + """
<div class=auth>
<h3>Telegram OTP</h3>
<form method=post>
<input name=code placeholder="12345" required>
<button>Verify</button>
</form>
</div>
""")

# ===================== DASHBOARD =====================
@app.route("/")
async def home():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT * FROM targets") as c:
            rows = await c.fetchall()
    return render_template_string(STYLE + """
<div class=auth>
<h3>Targets</h3>
{% for r in rows %}
<p>{{r[2]}} — {{r[3]}}</p>
{% endfor %}
<a href=/add>+ Add Target</a>
</div>
""", rows=rows)

# ===================== ADD TARGET =====================
@app.route("/add", methods=["GET","POST"])
async def add():
    if request.method == "POST":
        f = await request.form
        e = await client.get_entity(f["target"])
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT OR IGNORE INTO targets VALUES (?,?,?,?,?)",
                (e.id, e.username or "", f["target"], "checking", "new")
            )
            await db.commit()
        return redirect("/")
    return render_template_string(STYLE + """
<div class=auth>
<form method=post>
<input name=target placeholder="username / id" required>
<button>Add</button>
</form>
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
