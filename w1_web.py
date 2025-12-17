import os, sys, json, asyncio, logging, io, csv, secrets, hashlib, random, time, statistics, glob
from datetime import datetime, timedelta
import pytz
import aiosqlite
import python_socks
from quart import Quart, request, redirect, session, Response, render_template_string, url_for, flash, g, abort
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.tl.types import UserStatusOnline, InputPhoneContact
from telethon.tl.functions.contacts import ImportContactsRequest

# ===================== ⚙️ CONFIGURATION =====================
BASE_DIR = "."
DB_FILE = "tracker.db"
CONFIG_FILE = "config.json"
PIC_FOLDER = "static/profile_pics"
os.makedirs(PIC_FOLDER, exist_ok=True)

# 🛡️ Production Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Diamond")

DEFAULT_CONFIG = {
    "api_id": 0, "api_hash": "", "phone": "",
    "admin_username": "admin", "admin_password_hash": "",
    "recovery_key": secrets.token_hex(8), "secret_key": secrets.token_hex(32),
    "timezone": "Asia/Kolkata", "is_setup_done": False,
    "max_requests_per_min": 60,
    "db_retention_days": 60,
    "batch_size": 15,
    "proxy_enabled": False,
    "proxy_type": "socks5",
    "proxy_addr": "127.0.0.1",
    "proxy_port": 1080,
    "proxy_user": None,
    "proxy_pass": None
}

cfg = DEFAULT_CONFIG.copy()

# ===================== 🛡️ RATE GOVERNOR =====================
governor_lock = asyncio.Lock()
request_counter = 0
last_reset_time = time.time()

async def rate_governor(batch_size=1):
    global request_counter, last_reset_time
    async with governor_lock:
        now = time.time()
        if now - last_reset_time > 60:
            request_counter = 0; last_reset_time = now
        
        if request_counter + batch_size > cfg['max_requests_per_min']:
            wait = 60 - (now - last_reset_time) + 1
            logger.warning(f"🛡️ Rate Governor: Pausing {int(wait)}s")
            await asyncio.sleep(wait)
            request_counter = 0; last_reset_time = time.time()
        request_counter += batch_size

# ===================== 🔧 UTILS =====================
def load_config():
    global cfg
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                saved = json.load(f)
                for k, v in DEFAULT_CONFIG.items(): saved.setdefault(k, v)
                cfg = saved
        except: cfg = DEFAULT_CONFIG.copy()
    else: cfg = DEFAULT_CONFIG.copy(); save_config()

def save_config():
    try: with open(CONFIG_FILE, 'w') as f: json.dump(cfg, f, indent=4)
    except: pass

def hash_pwd(p): return hashlib.sha256((p + cfg['secret_key']).encode()).hexdigest()
def now_iso(): return datetime.now(pytz.timezone(cfg['timezone'])).isoformat()
def fmt_time(iso): 
    try: return datetime.fromisoformat(iso).strftime('%I:%M %p') if iso else "—"
    except: return "—"
def fmt_full(iso):
    try: return datetime.fromisoformat(iso).strftime('%d-%b %I:%M %p') if iso else "—"
    except: return "—"

load_config()

# ===================== 📡 TELEGRAM CLIENTS =====================
tracker_client = None
auth_client = None

def get_proxy():
    if not cfg.get('proxy_enabled'): return None
    ptype = python_socks.SOCKS5 if cfg['proxy_type'] == 'socks5' else python_socks.HTTP
    return (ptype, cfg['proxy_addr'], int(cfg['proxy_port']), True, cfg['proxy_user'], cfg['proxy_pass'])

def create_client(session_obj):
    if not cfg.get("api_id") or not cfg.get("api_hash"): raise ValueError("API Config Missing")
    try:
        return TelegramClient(session_obj, cfg["api_id"], cfg["api_hash"], proxy=get_proxy())
    except: return None

async def get_auth_client():
    global auth_client
    if auth_client and not auth_client.is_connected(): await auth_client.disconnect(); auth_client = None
    if auth_client is None:
        auth_client = create_client("session_pro")
        if auth_client: await auth_client.connect()
    return auth_client

async def reset_clients():
    global tracker_client, auth_client
    for c in [tracker_client, auth_client]:
        if c: 
            try: await c.disconnect()
            except: pass
    tracker_client = None; auth_client = None

async def download_pic(user_entity, tg):
    try:
        fname = f"{user_entity.id}_{secrets.token_hex(4)}.jpg"
        path = await tg.download_profile_photo(user_entity, file=os.path.join(PIC_FOLDER, fname))
        return fname if path else None
    except: return None

# ===================== 🚀 APP & DB =====================
app = Quart(__name__)
app.secret_key = cfg['secret_key']

async def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = await aiosqlite.connect(DB_FILE)
        await db.execute("PRAGMA journal_mode=WAL;")
    return db

@app.teardown_appcontext
async def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None: await db.close()

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute('''CREATE TABLE IF NOT EXISTS targets (
            user_id INTEGER PRIMARY KEY, username TEXT, display_name TEXT, 
            current_status TEXT, last_seen TEXT, pic_path TEXT,
            predicted_sleep TEXT, predicted_wake TEXT, online_prob INTEGER DEFAULT 0)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, status TEXT, 
            start_time TEXT, end_time TEXT, duration TEXT,
            FOREIGN KEY(user_id) REFERENCES targets(user_id))''')
        await db.execute('''CREATE TABLE IF NOT EXISTS status_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, 
            status TEXT, timestamp TEXT, FOREIGN KEY(user_id) REFERENCES targets(user_id))''')
        await db.execute("CREATE INDEX IF NOT EXISTS idx_session_uid ON sessions(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON status_events(timestamp)")
        await db.commit()

# ===================== 🧠 ANALYTICS =====================
def calc_duration(start, end):
    try:
        diff = datetime.fromisoformat(end) - datetime.fromisoformat(start)
        return str(timedelta(seconds=int(diff.total_seconds())))
    except: return "0s"

async def get_heatmap(user_id):
    hourly = [0]*24
    db = await get_db()
    async with db.execute("SELECT start_time, end_time FROM sessions WHERE user_id=? AND end_time IS NOT NULL ORDER BY id DESC LIMIT 200", (user_id,)) as c:
        sessions = await c.fetchall()
    for s, e in sessions:
        try:
            cur = datetime.fromisoformat(s); end = datetime.fromisoformat(e)
            while cur < end:
                hourly[cur.hour] += 1
                cur += timedelta(minutes=60)
        except: pass
    return [min(x*5, 60) for x in hourly]

# ===================== 🕵️ DIAMOND TRACKER ENGINE (VECTORIZED) =====================
async def process_batch(tg, batch):
    tasks = []
    for uid in batch: tasks.append(tg.get_entity(uid))
    return await asyncio.gather(*tasks, return_exceptions=True)

async def apply_updates_bulk(db, updates, batch_ids, ts):
    """
    💎 Diamond Optimization: 
    1 Query to fetch state of ALL users in batch.
    Zero N+1 queries.
    """
    # 1. Prepare Data
    valid_updates = []
    uids_to_check = []
    
    for i, res in enumerate(updates):
        uid = batch_ids[i]
        if isinstance(res, Exception):
            if isinstance(res, FloodWaitError): logger.warning(f"🌊 FloodWait {uid}: {res.seconds}s")
            continue
        
        status = 'online' if isinstance(res.status, UserStatusOnline) else 'offline'
        valid_updates.append((uid, status))
        uids_to_check.append(uid)

    if not uids_to_check: return

    # 2. Bulk Fetch Active Sessions (Vectorized Read)
    placeholder = ','.join('?' for _ in uids_to_check)
    async with db.execute(f'SELECT user_id, id, start_time FROM sessions WHERE end_time IS NULL AND user_id IN ({placeholder})', uids_to_check) as c:
        active_rows = await c.fetchall()
    
    # Map user_id -> (session_id, start_time)
    active_sessions = {row[0]: (row[1], row[2]) for row in active_rows}

    # 3. Calculate Operations in Memory
    target_updates = []
    new_sessions = []
    close_sessions = []
    new_events = []

    for uid, status in valid_updates:
        # Update Target Status
        if status == 'offline': target_updates.append((status, ts, uid))
        else: target_updates.append((status, None, uid)) # Don't overwrite last_seen if online

        # Logic
        is_active = uid in active_sessions
        
        if status == 'online' and not is_active:
            new_sessions.append((uid, 'ONLINE', ts))
            new_events.append((uid, 'ONLINE', ts))
        elif status == 'offline' and is_active:
            sid, start_t = active_sessions[uid]
            close_sessions.append((ts, calc_duration(start_t, ts), sid))
            new_events.append((uid, 'OFFLINE', ts))

    # 4. Bulk Write (Vectorized Write)
    if target_updates: 
        # Split updates because SQL logic differs slightly for online vs offline (last_seen)
        off_list = [x for x in target_updates if x[0] == 'offline']
        on_list = [(x[0], x[2]) for x in target_updates if x[0] == 'online']
        if off_list: await db.executemany('UPDATE targets SET current_status=?, last_seen=? WHERE user_id=?', off_list)
        if on_list: await db.executemany('UPDATE targets SET current_status=? WHERE user_id=?', on_list)

    if new_sessions: await db.executemany('INSERT INTO sessions (user_id, status, start_time) VALUES (?,?,?)', new_sessions)
    if close_sessions: await db.executemany('UPDATE sessions SET end_time=?, duration=? WHERE id=?', close_sessions)
    if new_events: await db.executemany('INSERT INTO status_events (user_id, status, timestamp) VALUES (?,?,?)', new_events)

async def maintenance(db):
    try:
        cutoff = (datetime.now() - timedelta(days=cfg['db_retention_days'])).isoformat()
        await db.execute("DELETE FROM sessions WHERE start_time < ?", (cutoff,))
        await db.execute("DELETE FROM status_events WHERE timestamp < ?", (cutoff,))
        await db.execute("VACUUM")
        await db.commit()
    except: pass

async def tracker_loop():
    global tracker_client
    db = await aiosqlite.connect(DB_FILE)
    await db.execute("PRAGMA journal_mode=WAL;") 
    last_maint = time.time()
    
    while True:
        try:
            if not tracker_client:
                s_str = os.environ.get("SESSION_STRING") or cfg.get("session_string")
                if s_str: 
                    try:
                        tracker_client = create_client(StringSession(s_str))
                        if tracker_client: await tracker_client.connect()
                    except Exception as e: logger.error(f"Tracker Connect Fail: {e}")
            
            if not tracker_client or not await tracker_client.is_user_authorized():
                await asyncio.sleep(10); continue

            if time.time() - last_maint > 3600:
                await maintenance(db)
                last_maint = time.time()

            async with db.execute('SELECT user_id FROM targets') as cursor:
                all_targets = [row[0] for row in await cursor.fetchall()]

            if not all_targets: await asyncio.sleep(5); continue

            chunk = cfg['batch_size']
            ts = now_iso()
            
            for i in range(0, len(all_targets), chunk):
                batch = all_targets[i : i + chunk]
                await rate_governor(len(batch))
                try:
                    results = await process_batch(tracker_client, batch)
                    await apply_updates_bulk(db, results, batch, ts)
                    await db.commit()
                except Exception as e: logger.debug(f"Batch Err: {e}")
                await asyncio.sleep(1)

            await asyncio.sleep(5)
            
    except Exception as e:
        logger.error(f"Fatal Loop Crash: {e}")
        await asyncio.sleep(5) # Prevent CPU spin if DB fails hard

# ===================== 🎨 UI =====================
STYLE = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Diamond Tracker</title><script src="https://cdn.jsdelivr.net/npm/chart.js"></script><link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet"><link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;800&display=swap" rel="stylesheet"><style>:root{--bg:#050505;--card:rgba(22,22,22,0.7);--border:rgba(255,255,255,0.08);--accent:#3b82f6;--text:#e5e5e5;--green:#10b981;--red:#ef4444;--glass:blur(16px)}body{margin:0;font-family:'Inter',sans-serif;background:radial-gradient(circle at 50% 0,#1f1f1f,#000);color:var(--text);min-height:100vh;display:flex;flex-direction:column;align-items:center}.container{width:92%;max-width:480px;margin:20px auto 80px}.card{background:var(--card);backdrop-filter:var(--glass);border:1px solid var(--border);border-radius:24px;padding:24px;box-shadow:0 20px 40px -10px rgba(0,0,0,0.6);margin-bottom:16px;animation:fadeUp 0.4s ease-out}.input{width:100%;padding:16px;background:#0a0a0a;border:1px solid #333;border-radius:14px;color:#fff;outline:0;box-sizing:border-box;font-size:16px}.input:focus{border-color:var(--accent)}.btn{width:100%;padding:16px;background:var(--accent);color:#fff;border:0;border-radius:14px;font-weight:700;cursor:pointer;font-size:16px}.nav{width:100%;padding:18px 24px;display:flex;justify-content:space-between;align-items:center;background:rgba(5,5,5,0.85);backdrop-filter:blur(12px);position:sticky;top:0;z-index:50;border-bottom:1px solid #222;box-sizing:border-box}.ava{width:52px;height:52px;border-radius:50%;margin-right:16px;object-fit:cover;background:#111}.ava.on{border:2px solid var(--green);animation:pulse 2s infinite}.badge{padding:6px 12px;border-radius:100px;font-size:0.75rem;font-weight:800;text-transform:uppercase}.b-on{background:rgba(16,185,129,0.15);color:var(--green)}.b-off{background:rgba(255,255,255,0.05);color:#666}.fab{position:fixed;bottom:30px;right:30px;width:60px;height:60px;background:var(--accent);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:1.6rem;color:#fff;box-shadow:0 12px 30px rgba(59,130,246,0.5);z-index:99}.pagination{display:flex;justify-content:center;gap:10px;margin-top:20px}@keyframes fadeUp{from{opacity:0;transform:translateY(15px)}to{opacity:1;transform:translateY(0)}}@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(16,185,129,0.6)}70%{box-shadow:0 0 0 12px rgba(16,185,129,0)}100%{box-shadow:0 0 0 0 rgba(16,185,129,0)}}</style></head><body>"""
FOOTER = "</body></html>"

@app.before_request
async def guard():
    if request.path.startswith('/static'): return
    if not cfg['is_setup_done'] and request.path not in ('/setup','/do_setup'): return redirect('/setup')
    if cfg['is_setup_done'] and 'user' not in session and request.path not in ('/login','/do_login','/reset','/do_reset'): return redirect('/login')
    if request.path in ('/connect','/auth','/verify','/2fa','/do_2fa','/logout'): return
    tg = await get_auth_client()
    if not tg or (tg.is_connected() and not await tg.is_user_authorized()): return redirect('/connect')

# CSRF Protection Hook (Simplified for single file)
@app.before_request
async def csrf_protect():
    if request.method == "POST":
        token = (await request.form).get('csrf_token')
        if not token or token != session.get('csrf_token'):
            # Allow initial setup without token
            if request.path not in ('/do_setup', '/do_login', '/do_reset'):
                return "Session Expired. Refresh."

def csrf():
    if 'csrf_token' not in session: session['csrf_token'] = secrets.token_hex(16)
    return f'<input type="hidden" name="csrf_token" value="{session["csrf_token"]}">'

@app.route('/setup')
async def setup(): return await render_template_string(STYLE+f"""<div class="container"><div class="card"><h2>Setup</h2><form action="/do_setup" method="post"><input name="u" class="input" placeholder="Admin Username" required><br><br><input type="password" name="p" class="input" placeholder="Password" required><br><br><button class="btn">Initialize</button></form></div></div>"""+FOOTER)

@app.route('/do_setup', methods=['POST'])
async def do_setup(): f=await request.form; cfg.update({'admin_username':f['u'],'admin_password_hash':hash_pwd(f['p']),'is_setup_done':True}); save_config(); return redirect('/login')

@app.route('/login')
async def login(): return await render_template_string(STYLE+f"""<div class="container"><div class="card"><h2>Login</h2><form action="/do_login" method="post"><input name="u" class="input" placeholder="Username" required><br><br><input type="password" name="p" class="input" placeholder="Password" required><br><br><button class="btn">Access</button></form><br><center><a href="/reset" style="color:#444;font-size:0.8rem">Recover</a></center></div></div>"""+FOOTER)

@app.route('/do_login', methods=['POST'])
async def do_login():
    f=await request.form
    if f['u']==cfg['admin_username'] and hash_pwd(f['p'])==cfg.get('admin_password_hash'): session['user']=f['u']; return redirect('/')
    return redirect('/login')

@app.route('/connect')
async def connect(): 
    if os.environ.get("SESSION_STRING"): return redirect('/')
    return await render_template_string(STYLE+f"""<div class="container"><div class="card"><h2>Connect</h2><form action="/auth" method="post">{csrf()}<input name="aid" type="number" class="input" placeholder="API ID" required><br><br><input name="hash" class="input" placeholder="API Hash" required><br><br><input name="ph" class="input" placeholder="+91..." required><br><br><button class="btn">OTP</button></form></div></div>"""+FOOTER)

@app.route('/auth', methods=['POST'])
async def auth(): f=await request.form; cfg.update({'api_id':int(f['aid']),'api_hash':f['hash'],'phone':f['ph']}); save_config(); tg=await get_auth_client(); await tg.send_code_request(cfg['phone']); return redirect('/verify')

@app.route('/verify')
async def verify(): return await render_template_string(STYLE+f"""<div class="container"><div class="card"><h2>OTP</h2><form action="/do_verify" method="post">{csrf()}<input name="c" type="number" class="input" placeholder="Code" required><br><br><button class="btn">Link</button></form></div></div>"""+FOOTER)

@app.route('/do_verify', methods=['POST'])
async def do_verify():
    f=await request.form; tg=await get_auth_client()
    try:
        await tg.sign_in(phone=cfg['phone'], code=f['c'])
        cfg['session_string']=tg.session.save(); save_config(); await reset_clients(); return redirect('/')
    except SessionPasswordNeededError: return redirect('/2fa')
    except Exception as e: return f"Error: {e}"

@app.route('/2fa')
async def two_fa(): return await render_template_string(STYLE+f"""<div class="container"><div class="card"><h2>2FA Required</h2><form action="/do_2fa" method="post">{csrf()}<input type="password" name="pw" class="input" placeholder="Cloud Password" required><br><br><button class="btn">Unlock</button></form></div></div>"""+FOOTER)

@app.route('/do_2fa', methods=['POST'])
async def do_2fa():
    f=await request.form; tg=await get_auth_client()
    try:
        await tg.sign_in(password=f['pw'])
        cfg['session_string']=tg.session.save(); save_config(); await reset_clients(); return redirect('/')
    except Exception as e: return f"Error: {e}"

@app.route('/')
async def home():
    page = int(request.args.get('page', 1)); offset = (page - 1) * 50
    db = await get_db(); db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM targets ORDER BY CASE WHEN current_status='online' THEN 0 ELSE 1 END, last_seen DESC LIMIT 50 OFFSET ?", (offset,)) as c: rows=await c.fetchall()
    cards = ""
    for r in rows:
        pic = f"/static/profile_pics/{r['pic_path']}" if r['pic_path'] else f"https://ui-avatars.com/api/?name={r['display_name']}&background=random&color=fff"
        cards += f"""<a href="/target/{r['user_id']}"><div class="card" style="display:flex;align-items:center"><img src="{pic}" class="ava {'on' if r['current_status']=='online' else ''}"><div><div style="font-weight:700;font-size:1.1rem">{r['display_name']}</div><div style="font-size:0.75rem;color:#777">{fmt_time(r['last_seen'])}</div></div><div style="margin-left:auto" class="badge {'b-on' if r['current_status']=='online' else 'b-off'}">{r['current_status']}</div></div></a>"""
    
    pagination = f"""<div class="pagination">{f'<a href="/?page={page-1}" class="btn" style="padding:10px">←</a>' if page > 1 else ''}<span style="align-self:center;color:#666">Page {page}</span><a href="/?page={page+1}" class="btn" style="padding:10px">→</a></div>"""
    return await render_template_string(STYLE+f"""<div class="nav"><div style="font-weight:800"><i class="fas fa-eye"></i> DIAMOND</div><a href="/profile" style="color:#888"><i class="fas fa-cog"></i></a></div><div class="container"><div style="text-align:right;color:#444;font-size:0.7rem;margin-bottom:10px">Scan: {datetime.now().strftime('%H:%M')}</div>{cards if cards else "<center style='color:#444'>No Targets</center>"}{pagination}</div><a href="/add" class="fab"><i class="fas fa-plus"></i></a>"""+FOOTER)

@app.route('/target/<int:uid>')
async def target(uid):
    db = await get_db(); db.row_factory = aiosqlite.Row
    async with db.execute('SELECT * FROM targets WHERE user_id=?',(uid,)) as c: t=await c.fetchone()
    if not t: return redirect('/')
    heat = await get_heatmap(uid)
    return await render_template_string(STYLE+f"""<div class="nav"><a href="/" style="color:#fff"><i class="fas fa-arrow-left"></i></a><b>{t['display_name']}</b><a href="/del/{uid}" style="color:var(--red)"><i class="fas fa-trash"></i></a></div><div class="container"><div class="card" style="text-align:center"><img src="/static/profile_pics/{t['pic_path']}" class="ava {'on' if t['current_status']=='online' else ''}" style="width:80px;height:80px;margin:0 auto 10px"><h2 style="font-size:1.5rem">{t['display_name']}</h2><div class="stat-grid"><div class="stat-box"><div class="stat-val">{t['online_prob']}%</div><div class="stat-lbl">Prob</div></div><div class="stat-box"><div class="stat-val">{t['predicted_sleep']}</div><div class="stat-lbl">Sleep</div></div></div></div><div class="card"><h2>Activity</h2><div style="height:180px"><canvas id="c"></canvas></div></div><a href="/export/{uid}" class="btn" style="background:#222;display:block;text-align:center">Log</a></div><script>var ctx=document.getElementById('c'); if(window.cChart) window.cChart.destroy(); window.cChart=new Chart(ctx,{{type:'bar',data:{{labels:[...Array(24).keys()].map(x=>x+":00"),datasets:[{{label:'Mins',data:{heat},backgroundColor:'#3b82f6',borderRadius:6,barPercentage:0.8}}]}},options:{{scales:{{x:{{display:true,grid:{{display:false}}}},y:{{display:true,grid:{{color:'#333'}} }} }} }} }});</script>"""+FOOTER)

@app.route('/add', methods=['GET','POST'])
async def add():
    if request.method=='GET': return await render_template_string(STYLE+f"""<div class="container"><div class="card"><h2>Track</h2><form method="post">{csrf()}<input name="t" class="input" placeholder="User/+91.." required><br><br><input name="n" class="input" placeholder="Alias"><br><br><button class="btn">Add</button></form></div></div>"""+FOOTER)
    f=await request.form; tg=await get_auth_client()
    try:
        if f['t'].strip().isdigit() or f['t'].startswith('+'): c=InputPhoneContact(0,f['t'],f['n'],""); r=await tg(ImportContactsRequest([c])); e=r.users[0]
        else: e=await tg.get_entity(f['t'])
        pic=await download_pic(e,tg); db = await get_db()
        await db.execute('INSERT OR IGNORE INTO targets (user_id,username,display_name,current_status,last_seen,pic_path) VALUES (?,?,?,?,?,?)',(e.id,getattr(e,'username',''),f['n'] or f['t'],'Scanning',now_iso(),pic)); await db.commit()
        return redirect('/')
    except: return "Error"

@app.route('/del/<int:uid>')
async def delete(uid): db = await get_db(); await db.execute('DELETE FROM targets WHERE user_id=?',(uid,)); await db.commit(); return redirect('/')

@app.route('/export/<int:uid>')
async def export(uid):
    db = await get_db(); async with db.execute('SELECT * FROM sessions WHERE user_id=? ORDER BY id DESC',(uid,)) as c: rows=await c.fetchall()
    si=io.StringIO(); cw=csv.writer(si); cw.writerow(['ID','User','Status','Start','End','Dur']); [cw.writerow([r[0],r[1],r[2],fmt_full(r[3]),fmt_full(r[4]),r[5]]) for r in rows]
    return Response(si.getvalue(), mimetype='text/csv', headers={"Content-Disposition":f"attachment; filename=log_{uid}.csv"})

@app.route('/logout')
async def logout(): await reset_clients(); session.clear(); return redirect('/login')

@app.route('/reset')
async def reset(): return await render_template_string(STYLE+f"""<div class="container"><div class="card"><h2>Reset</h2><form action="/do_reset" method="post">{csrf()}<input name="k" class="input" placeholder="Key" required><br><br><button class="btn" style="background:#444">Reset</button></form></div></div>"""+FOOTER)

@app.route('/do_reset', methods=['POST'])
async def do_reset():
    if (await request.form)['k']==cfg['recovery_key']: await reset_clients(); cfg['is_setup_done']=False; save_config(); return redirect('/setup')
    return "Invalid"

@app.before_serving
async def start(): await init_db(); app.add_background_task(tracker_loop)

if __name__ == '__main__':
    from hypercorn.config import Config; import hypercorn.asyncio
    c=Config(); c.bind=[f"0.0.0.0:{os.environ.get('PORT',10000)}"]; asyncio.run(hypercorn.asyncio.serve(app,c))
