import os
import sys
import json
import asyncio
import logging
import io
import csv
import secrets
import hashlib
import time
from datetime import datetime, timedelta
from typing import Optional, List

# Third-party async libraries
try:
    import pytz
    import aiosqlite
    import python_socks
    from quart import (
        Quart, request, redirect, session, Response, 
        render_template_string, flash, g, jsonify
    )
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.errors import FloodWaitError, SessionPasswordNeededError
    from telethon.tl.types import UserStatusOnline, InputPhoneContact
    from telethon.tl.functions.contacts import ImportContactsRequest
except ImportError as e:
    print(f"CRITICAL: Missing dependency. {e}")
    sys.exit(1)

# ===================== ⚙️ CONFIGURATION =====================
DB_FILE = "tracker.db"
CONFIG_FILE = "config.json"
PIC_FOLDER = "static/profile_pics"

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(name)s | %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger("DiamondCore")

DEFAULT_CONFIG = {
    "api_id": 0, "api_hash": "", "session_string": "",
    "admin_username": "admin", "admin_password_hash": "",
    "secret_key": secrets.token_hex(32), "timezone": "UTC", "is_setup_done": False,
    "max_requests_per_min": 50, "db_retention_days": 90, "batch_size": 15,
    "proxy_enabled": False, "proxy_type": "socks5", "proxy_addr": "127.0.0.1",
    "proxy_port": 1080, "proxy_user": None, "proxy_pass": None
}
cfg = DEFAULT_CONFIG.copy()

# ===================== 🛠️ UTILITIES =====================
def load_config():
    global cfg
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                saved = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    if k not in saved: saved[k] = v
                cfg = saved
        except: pass
    
    if os.environ.get("API_ID"): cfg["api_id"] = int(os.environ["API_ID"])
    if os.environ.get("API_HASH"): cfg["api_hash"] = os.environ["API_HASH"]
    if os.environ.get("SESSION_STRING"): cfg["session_string"] = os.environ["SESSION_STRING"]
    if os.environ.get("SECRET_KEY"): cfg["secret_key"] = os.environ["SECRET_KEY"]
    if not cfg["secret_key"]: cfg["secret_key"] = secrets.token_hex(32)
    save_config()

def save_config():
    if 'session_string' in cfg: os.environ['SESSION_STRING'] = cfg['session_string']
    if os.access('.', os.W_OK):
        try:
            with open(CONFIG_FILE, 'w') as f: json.dump(cfg, f, indent=4)
        except: pass

def hash_pwd(p): return hashlib.sha256((p + cfg['secret_key']).encode()).hexdigest()
def clean_html(t): return str(t).replace("<", "&lt;").replace(">", "&gt;") if t else ""
def now_iso(): return datetime.now(pytz.timezone(cfg['timezone'])).isoformat()
def fmt_time(iso): return datetime.fromisoformat(iso).strftime('%I:%M %p') if iso else "—"
def fmt_full(iso): return datetime.fromisoformat(iso).strftime('%d-%b %I:%M %p') if iso else "—"

load_config()

# ===================== 📡 TELETHON =====================
tracker_client = None
auth_client = None

def get_proxy():
    if not cfg.get('proxy_enabled'): return None
    ptype = python_socks.SOCKS5 if cfg['proxy_type'] == 'socks5' else python_socks.HTTP
    return (ptype, cfg['proxy_addr'], int(cfg['proxy_port']), True, cfg['proxy_user'], cfg['proxy_pass'])

def create_client(session, auth=False):
    if not cfg.get("api_id") or not cfg.get("api_hash"): return None
    try: return TelegramClient(session, int(cfg["api_id"]), cfg["api_hash"], proxy=get_proxy(), connection_retries=3, auto_reconnect=True)
    except: return None

async def get_auth_client():
    global auth_client
    if auth_client and not auth_client.is_connected(): await auth_client.disconnect(); auth_client = None
    if auth_client is None:
        auth_client = create_client(StringSession(""), auth=True)
        if auth_client: await auth_client.connect()
    return auth_client

async def reset_clients():
    global tracker_client, auth_client
    if tracker_client: await tracker_client.disconnect()
    if auth_client: await auth_client.disconnect()
    tracker_client = None; auth_client = None

async def download_pic(entity, tg):
    try:
        if not os.path.exists(PIC_FOLDER): os.makedirs(PIC_FOLDER, exist_ok=True)
        fname = f"{entity.id}_{secrets.token_hex(4)}.jpg"
        path = await tg.download_profile_photo(entity, file=os.path.join(PIC_FOLDER, fname))
        return fname if path else None
    except: return None

# ===================== 🚀 APP =====================
app = Quart(__name__)
app.secret_key = cfg['secret_key']

async def get_db():
    if not hasattr(g, '_database'):
        g._database = await aiosqlite.connect(DB_FILE)
        await g._database.execute("PRAGMA journal_mode=WAL;")
    return g._database

@app.teardown_appcontext
async def close_db(e):
    db = getattr(g, '_database', None)
    if db: await db.close()

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("CREATE TABLE IF NOT EXISTS targets (user_id INTEGER PRIMARY KEY, username TEXT, display_name TEXT, current_status TEXT, last_seen TEXT, pic_path TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, user_id INTEGER, status TEXT, start_time TEXT, end_time TEXT, duration TEXT)")
        await db.commit()

# ===================== 🛡️ MIDDLEWARE =====================
def csrf_field():
    if 'csrf_token' not in session: session['csrf_token'] = secrets.token_hex(16)
    return f'<input type="hidden" name="csrf_token" value="{session["csrf_token"]}">'

@app.before_request
async def security():
    if request.method == "POST":
        public = ('/do_login', '/do_setup', '/auth', '/verify', '/do_verify', '/do_2fa')
        if request.path not in public:
            form = await request.form
            if form.get('csrf_token') != session.get('csrf_token'): return "Invalid CSRF", 403

    if request.path.startswith('/static') or request.path in ('/manifest.json', '/service-worker.js'): return
    public_paths = ('/setup', '/do_setup', '/login', '/do_login', '/connect', '/auth', '/verify', '/do_verify', '/2fa', '/do_2fa')
    
    if not cfg['is_setup_done'] and request.path not in public_paths: return redirect('/setup')
    if 'user' not in session and request.path not in public_paths: return redirect('/login')

# ===================== 🕵️ TRACKER =====================
async def tracker_loop():
    global tracker_client
    while True:
        try:
            async with aiosqlite.connect(DB_FILE) as db:
                await db.execute("PRAGMA journal_mode=WAL;")
                while True:
                    if not tracker_client:
                        s = os.environ.get("SESSION_STRING") or cfg.get("session_string")
                        if s: 
                            tracker_client = create_client(StringSession(s))
                            if tracker_client: await tracker_client.connect()
                    
                    if not tracker_client or not await tracker_client.is_user_authorized():
                        await asyncio.sleep(15); continue

                    async with db.execute('SELECT user_id FROM targets') as cursor:
                        targets = [r[0] for r in await cursor.fetchall()]

                    if not targets: await asyncio.sleep(5); continue

                    ts = now_iso()
                    tasks = [tracker_client.get_entity(uid) for uid in targets[:15]] # Batch 15
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    for i, res in enumerate(results):
                        if isinstance(res, Exception): continue
                        uid = targets[i]
                        status = 'online' if isinstance(res.status, UserStatusOnline) else 'offline'
                        
                        # Logic: Update Status & Sessions
                        await db.execute('UPDATE targets SET current_status=?, last_seen=? WHERE user_id=?', (status, ts, uid))
                        
                        # Check open session
                        async with db.execute('SELECT id, start_time FROM sessions WHERE user_id=? AND end_time IS NULL', (uid,)) as c:
                            open_session = await c.fetchone()
                        
                        if status == 'online' and not open_session:
                            await db.execute('INSERT INTO sessions (user_id, status, start_time) VALUES (?,?,?)', (uid, 'ONLINE', ts))
                        elif status == 'offline' and open_session:
                            sid, start_t = open_session
                            dur = str(timedelta(seconds=int((datetime.fromisoformat(ts) - datetime.fromisoformat(start_t)).total_seconds())))
                            await db.execute('UPDATE sessions SET end_time=?, duration=? WHERE id=?', (ts, dur, sid))
                    
                    await db.commit()
                    await asyncio.sleep(2) # Scan interval
        except:
            await reset_clients(); await asyncio.sleep(10)

# ===================== 💎 UI =====================
LAYOUT = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Diamond Tracker</title><link rel="manifest" href="/manifest.json"><script src="https://cdn.jsdelivr.net/npm/chart.js"></script><style>body{background:#050505;color:#e5e5e5;font-family:sans-serif;margin:0;padding:20px}.card{background:#222;padding:20px;border-radius:15px;margin-bottom:15px}.btn{background:#3b82f6;color:#fff;padding:10px 20px;border:none;border-radius:8px;width:100%;font-size:16px}.input{width:100%;padding:10px;background:#333;border:1px solid #444;color:#fff;border-radius:8px;box-sizing:border-box}.on{border:2px solid #10b981}.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#222;padding:10px 20px;border-radius:10px;border:1px solid #444}</style></head><body><div id="toast"></div><script>if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js');window.onload=()=>{% with m=get_flashed_messages() %}{% if m %}document.getElementById('toast').innerText="{{m[0]}}";setTimeout(()=>document.getElementById('toast').remove(),3000);{% endif %}{% endwith %}</script>{% block c %}{% endblock %}</body></html>"""

def render(c): return render_template_string(LAYOUT.replace("{% block c %}{% endblock %}", c))

@app.route('/manifest.json')
async def manifest(): return jsonify({"name":"Diamond","display":"standalone","start_url":"/","icons":[{"src":"https://ui-avatars.com/api/?name=D&background=000&color=fff","sizes":"192x192","type":"image/png"}]})

@app.route('/sw.js')
async def sw(): return Response("self.addEventListener('fetch',e=>{})", mimetype='application/javascript')

@app.route('/')
async def home():
    db = await get_db(); db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM targets ORDER BY CASE WHEN current_status='online' THEN 0 ELSE 1 END") as c: rows = await c.fetchall()
    html = ""
    for r in rows:
        pic = f"/static/profile_pics/{r['pic_path']}" if r['pic_path'] else f"https://ui-avatars.com/api/?name={r['display_name']}"
        html += f"""<a href='/t/{r['user_id']}'><div class='card' style='display:flex;align-items:center;gap:15px'><img src='{pic}' style='width:50px;height:50px;border-radius:50%' class='{ 'on' if r['current_status']=='online' else ''}'><div><h3>{clean_html(r['display_name'])}</h3><small>{fmt_time(r['last_seen'])}</small></div><b style='margin-left:auto;color:{'#10b981' if r['current_status']=='online' else '#666'}'>{r['current_status']}</b></div></a>"""
    return await render(f"<h1>Diamond Tracker</h1>{html}<br><a href='/add'><button class='btn'>+ Add Target</button></a><br><br><a href='/logout' style='color:#666'>Logout</a>")

@app.route('/login')
async def login(): return await render(f"<div class='card'><h2>Login</h2><form action='/do_login' method='post'>{csrf_field()}<input name='u' class='input' placeholder='User'><br><br><input type='password' name='p' class='input' placeholder='Pass'><br><br><button class='btn'>Login</button></form></div>")

@app.route('/do_login', methods=['POST'])
async def do_login():
    f = await request.form
    if f['u'] == cfg['admin_username'] and hash_pwd(f['p']) == cfg['admin_password_hash']:
        session['user'] = f['u']; return redirect('/')
    await flash("Invalid"); return redirect('/login')

@app.route('/setup')
async def setup(): return await render(f"<div class='card'><h2>Setup</h2><form action='/do_setup' method='post'>{csrf_field()}<input name='u' class='input' placeholder='New Username'><br><br><input type='password' name='p' class='input' placeholder='New Password'><br><br><button class='btn'>Install</button></form></div>")

@app.route('/do_setup', methods=['POST'])
async def do_setup():
    f = await request.form; cfg.update({'admin_username':f['u'], 'admin_password_hash':hash_pwd(f['p']), 'is_setup_done':True}); save_config()
    return redirect('/login')

@app.route('/add', methods=['GET','POST'])
async def add():
    if request.method == 'GET': return await render(f"<div class='card'><h2>Track User</h2><form method='post'>{csrf_field()}<input name='t' class='input' placeholder='Phone or Username'><br><br><input name='n' class='input' placeholder='Name'><br><br><button class='btn'>Track</button></form></div>")
    f = await request.form; tg = await get_auth_client()
    try:
        val = f['t'].strip(); 
        entity = (await tg(ImportContactsRequest([InputPhoneContact(0, val, f['n'] or val, "")]))).users[0] if val[0].isdigit() or val.startswith('+') else await tg.get_entity(val)
        pic = await download_pic(entity, tg)
        db = await get_db(); await db.execute('INSERT OR REPLACE INTO targets (user_id, username, display_name, current_status, last_seen, pic_path) VALUES (?,?,?,?,?,?)', (entity.id, getattr(entity,'username',''), f['n'] or val, 'offline', now_iso(), pic)); await db.commit()
        return redirect('/')
    except Exception as e: await flash(str(e)); return redirect('/add')

@app.route('/connect')
async def connect(): return await render(f"<div class='card'><h2>Link Telegram</h2><form action='/auth' method='post'>{csrf_field()}<input name='aid' class='input' placeholder='API ID'><br><br><input name='hash' class='input' placeholder='API Hash'><br><br><input name='ph' class='input' placeholder='Phone'><br><br><button class='btn'>Get OTP</button></form></div>")

@app.route('/auth', methods=['POST'])
async def auth():
    f = await request.form; cfg.update({'api_id':f['aid'], 'api_hash':f['hash'], 'phone':f['ph']}); save_config()
    tg = await get_auth_client(); await tg.send_code_request(cfg['phone']); return redirect('/verify')

@app.route('/verify')
async def verify(): return await render(f"<div class='card'><h2>Enter OTP</h2><form action='/do_verify' method='post'>{csrf_field()}<input name='c' class='input' placeholder='12345'><br><br><button class='btn'>Submit</button></form></div>")

@app.route('/do_verify', methods=['POST'])
async def do_verify():
    f = await request.form; tg = await get_auth_client()
    try: await tg.sign_in(cfg['phone'], f['c']); cfg['session_string'] = tg.session.save(); save_config(); await reset_clients(); return redirect('/')
    except SessionPasswordNeededError: return redirect('/2fa')

@app.route('/2fa')
async def two_fa(): return await render(f"<div class='card'><h2>2FA Password</h2><form action='/do_2fa' method='post'>{csrf_field()}<input type='password' name='pw' class='input'><br><br><button class='btn'>Unlock</button></form></div>")

@app.route('/do_2fa', methods=['POST'])
async def do_2fa():
    tg = await get_auth_client(); await tg.sign_in(password=(await request.form)['pw']); cfg['session_string'] = tg.session.save(); save_config(); await reset_clients(); return redirect('/')

@app.route('/t/<int:uid>')
async def target(uid):
    db = await get_db(); db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM targets WHERE user_id=?",(uid,)) as c: t = await c.fetchone()
    async with db.execute("SELECT * FROM sessions WHERE user_id=? ORDER BY id DESC LIMIT 50",(uid,)) as c: log = await c.fetchall()
    
    log_html = "".join([f"<tr><td>{r['status']}</td><td>{fmt_time(r['start_time'])}</td><td>{r['duration'] or '-'}</td></tr>" for r in log])
    return await render(f"<div class='card'><h2>{clean_html(t['display_name'])}</h2><h3>History</h3><table width='100%' style='color:#ccc'><tr><th align='left'>Status</th><th align='left'>Time</th><th align='left'>Dur</th></tr>{log_html}</table><br><a href='/del/{uid}' style='color:red'>Delete Target</a></div>")

@app.route('/del/<int:uid>')
async def delete(uid):
    db = await get_db(); await db.execute("DELETE FROM targets WHERE user_id=?",(uid,)); await db.commit()
    return redirect('/')

@app.route('/logout')
async def logout(): session.clear(); return redirect('/login')

@app.before_serving
async def startup():
    if not os.path.exists(PIC_FOLDER): os.makedirs(PIC_FOLDER)
    await init_db()
    async def run():
        await asyncio.sleep(10)
        while True:
            try: await tracker_loop()
            except: await asyncio.sleep(10)
    app.add_background_task(run)

if __name__ == '__main__':
    from hypercorn.config import Config; import hypercorn.asyncio
    c = Config(); c.bind = [f"0.0.0.0:{os.environ.get('PORT', 10000)}"]
    asyncio.run(hypercorn.asyncio.serve(app, c))
