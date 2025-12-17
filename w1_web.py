import os, sys, json, asyncio, logging, io, csv, secrets
from datetime import datetime
import pytz
import aiosqlite
import python_socks
from quart import Quart, request, redirect, session, Response, render_template_string, url_for
from telethon import TelegramClient
from telethon.tl.types import UserStatusOnline, InputPhoneContact
from telethon.tl.functions.contacts import ImportContactsRequest

# ===================== PATHS (RENDER SAFE) =====================
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
    "phone": "",
    "admin_username": "admin",
    "admin_password": "password",
    "timezone": "Asia/Kolkata",
    "recovery_key": secrets.token_hex(8),
    "secret_key": secrets.token_hex(16),
    "is_setup_done": False
}

# ===================== CONFIG LOAD/SAVE =====================
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f: 
                c = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    c.setdefault(k, v)
                return c
        except: return DEFAULT_CONFIG.copy()
    return DEFAULT_CONFIG.copy()

def save_config(new_config):
    with open(CONFIG_FILE, 'w') as f: json.dump(new_config, f, indent=4)

cfg = load_config()

# ===================== TELEGRAM CLIENT =====================
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
app = Quart(__name__, static_folder=PIC_FOLDER, static_url_path='/static/profile_pics')
app.secret_key = cfg['secret_key']

# ===================== DATABASE & HELPERS =====================
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS targets (
            user_id INTEGER PRIMARY KEY, username TEXT, display_name TEXT, 
            current_status TEXT, last_seen TEXT, pic_path TEXT)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, status TEXT, 
            start_time TEXT, end_time TEXT, duration TEXT,
            FOREIGN KEY(user_id) REFERENCES targets(user_id))''')
        await db.commit()

def now_str():
    return datetime.now(pytz.timezone(cfg['timezone'])).strftime('%I:%M %p')

async def get_hourly_data(user_id):
    hourly_counts = [0] * 24
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute('SELECT start_time FROM sessions WHERE user_id = ?', (user_id,)) as cursor: 
            rows = await cursor.fetchall()
    for row in rows:
        try: hourly_counts[datetime.strptime(row[0], '%I:%M %p').hour] += 1
        except: pass
    return hourly_counts

async def get_ai_insight(user_id):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute('SELECT start_time FROM sessions WHERE user_id = ? ORDER BY id DESC LIMIT 50', (user_id,)) as c:
            sessions = await c.fetchall()
    if not sessions: return "No data yet."
    hours = [datetime.strptime(s[0], '%I:%M %p').hour for s in sessions if s[0]]
    if not hours: return "Analyzing..."
    from collections import Counter
    peak = Counter(hours).most_common(1)[0][0]
    peak_str = datetime.strptime(str(peak), "%H").strftime("%I %p")
    return f"Most active around {peak_str}"

async def download_pic(user_entity, tg):
    try:
        path = await tg.download_profile_photo(user_entity, file=PIC_FOLDER)
        if path:
            filename = os.path.basename(path)
            # Update DB with new pic
            async with aiosqlite.connect(DB_FILE) as db:
                await db.execute('UPDATE targets SET pic_path = ? WHERE user_id = ?', (filename, user_entity.id))
                await db.commit()
            return filename
    except: pass
    return "default.png"

# ===================== CORE TRACKER LOGIC (OPTIMIZED SPEED) =====================
async def tracker_loop():
    while True:
        try:
            tg = get_client()
            if not tg or not tg.is_connected() or not await tg.is_user_authorized():
                await asyncio.sleep(2)
                continue

            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute('SELECT user_id, display_name FROM targets') as cursor:
                    targets = await cursor.fetchall()

            if not targets:
                await asyncio.sleep(2)
                continue
            
            for (uid, name) in targets:
                try:
                    u = await tg.get_entity(uid)
                    status = 'online' if isinstance(u.status, UserStatusOnline) else 'offline'
                    current_time = now_str()

                    # Update "Last Seen"
                    async with aiosqlite.connect(DB_FILE) as db:
                        await db.execute('UPDATE targets SET current_status = ?, last_seen = ? WHERE user_id = ?', (status, current_time, uid))
                        await db.commit()

                    # Session Logic
                    if status == 'online':
                        async with aiosqlite.connect(DB_FILE) as db:
                            async with db.execute('SELECT id FROM sessions WHERE user_id = ? AND end_time IS NULL ORDER BY id DESC LIMIT 1', (uid,)) as c:
                                open_session = await c.fetchone()
                            
                            if not open_session:
                                await db.execute('INSERT INTO sessions (user_id, status, start_time) VALUES (?, ?, ?)', (uid, 'ONLINE', current_time))
                                await db.commit()
                    else:
                        async with aiosqlite.connect(DB_FILE) as db:
                             await db.execute('UPDATE sessions SET end_time = ? WHERE user_id = ? AND end_time IS NULL', (current_time, uid))
                             await db.commit()
                except Exception as e:
                    pass
                
                # ⚡ FAST MODE: Only wait 0.1s between users (was 1.0s)
                await asyncio.sleep(0.1) 
            
            # ⚡ FAST MODE: Only wait 1.5s before restarting loop (was 5.0s)
            await asyncio.sleep(1.5) 
        except:
            await asyncio.sleep(2)

# ===================== UI STYLES (GLASSMORPHISM) =====================
STYLE = """
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
<style>
:root { --bg: #0f172a; --card-bg: rgba(30, 41, 59, 0.7); --primary: #3b82f6; --text: #f1f5f9; --text-sub: #94a3b8; --border: rgba(255, 255, 255, 0.1); }
body { background: radial-gradient(circle at top, #1e293b, #0f172a); color: var(--text); font-family: 'Inter', sans-serif; margin: 0; min-height: 100vh; display: flex; flex-direction: column; align-items: center; }
a { text-decoration: none; color: inherit; }

/* Animations */
@keyframes fadeIn { from { opacity:0; transform:translateY(10px); } to { opacity:1; transform:translateY(0); } }
@keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.4); } 70% { box-shadow: 0 0 0 10px rgba(34, 197, 94, 0); } 100% { box-shadow: 0 0 0 0 rgba(34, 197, 94, 0); } }

/* Components */
.glass-container { background: var(--card-bg); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border: 1px solid var(--border); border-radius: 20px; padding: 2rem; width: 90%; max-width: 420px; box-shadow: 0 20px 25px rgba(0,0,0,0.3); animation: fadeIn 0.5s; margin-top: 5vh; }
.input { width: 100%; padding: 14px; margin-bottom: 12px; background: rgba(15,23,42,0.6); border: 1px solid var(--border); border-radius: 12px; color: white; box-sizing: border-box; font-size: 16px; }
.input:focus { outline: none; border-color: var(--primary); }
.btn { width: 100%; padding: 14px; background: var(--primary); color: white; border: none; border-radius: 12px; font-weight: 600; cursor: pointer; font-size: 16px; transition: 0.2s; }
.btn:hover { filter: brightness(1.1); }
.nav { width: 100%; padding: 15px 20px; display: flex; justify-content: space-between; align-items: center; background: rgba(15,23,42,0.8); backdrop-filter: blur(10px); border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 10; box-sizing: border-box; }

/* Cards */
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 15px; width: 95%; max-width: 1000px; padding: 20px 0; padding-bottom: 80px; }
.card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 16px; padding: 16px; display: flex; align-items: center; justify-content: space-between; transition: transform 0.2s; position: relative; }
.card:active { transform: scale(0.99); }
.avatar { width: 50px; height: 50px; border-radius: 50%; object-fit: cover; border: 2px solid #334155; margin-right: 15px; }
.avatar.online { border-color: #22c55e; }
.status-badge { font-size: 0.75rem; font-weight: 700; padding: 4px 10px; border-radius: 20px; text-transform: uppercase; display: flex; align-items: center; gap: 5px; }
.online-badge { background: rgba(34, 197, 94, 0.2); color: #4ade80; border: 1px solid rgba(34, 197, 94, 0.3); }
.offline-badge { background: rgba(148, 163, 184, 0.1); color: #94a3b8; }
.dot { width: 8px; height: 8px; background: #4ade80; border-radius: 50%; animation: pulse 2s infinite; }

/* FAB */
.fab { position: fixed; bottom: 25px; right: 25px; background: var(--primary); width: 60px; height: 60px; border-radius: 50%; display: flex; align-items: center; justify-content: center; color: white; font-size: 24px; box-shadow: 0 10px 20px rgba(0,0,0,0.4); transition: 0.2s; }
.fab:active { transform: scale(0.9); }
</style>
"""

# ===================== ROUTES =====================

# --- AUTH & SETUP ---
@app.route('/setup')
async def setup():
    if cfg['is_setup_done']: return redirect('/login')
    return await render_template_string(STYLE + """
<div class="glass-container">
    <h3 style="text-align:center">🚀 Tracker Setup</h3>
    <form action="/do_setup" method="post">
        <label style="color:var(--text-sub); font-size:0.9rem">Telegram API</label>
        <input name="api_id" type="number" class="input" placeholder="App ID" required>
        <input name="api_hash" class="input" placeholder="App Hash" required>
        <hr style="border-color:var(--border); margin: 20px 0">
        <label style="color:var(--text-sub); font-size:0.9rem">Admin Account</label>
        <input name="username" class="input" placeholder="Create Username" required>
        <input type="password" name="password" class="input" placeholder="Create Password" required>
        <button class="btn">Initialize System</button>
    </form>
</div>
""")

@app.route('/do_setup', methods=['POST'])
async def do_setup():
    global cfg
    f = await request.form
    cfg.update({
        "api_id": int(f['api_id']),
        "api_hash": f['api_hash'],
        "admin_username": f['username'],
        "admin_password": f['password'],
        "is_setup_done": True
    })
    save_config(cfg)
    get_client()
    return redirect('/login')

@app.route('/login')
async def login():
    return await render_template_string(STYLE + """
<div class="glass-container">
    <h3 style="text-align:center">🔐 Admin Login</h3>
    <form action="/do_login" method="post">
        <input name="username" class="input" placeholder="Username" required>
        <input type="password" name="password" class="input" placeholder="Password" required>
        <button class="btn">Access Dashboard</button>
    </form>
    <a href="/reset" style="display:block; text-align:center; margin-top:20px; color:#ef4444; font-size:0.8rem">Factory Reset</a>
</div>
""")

@app.route('/do_login', methods=['POST'])
async def do_login():
    f = await request.form
    if f['username'] == cfg['admin_username'] and f['password'] == cfg['admin_password']:
        session['user'] = f['username']
        return redirect('/')
    return redirect('/login')

# --- TELEGRAM CONNECT FLOW ---
@app.route('/enter-phone')
async def enter_phone():
    return await render_template_string(STYLE + """
<div class="glass-container">
    <h3 style="text-align:center">📱 Connect Telegram</h3>
    <p style="text-align:center; color:var(--text-sub)">Enter your phone number to start.</p>
    <form action="/send-code" method="post">
        <input name="phone" class="input" placeholder="+919876543210" required>
        <button class="btn">Send OTP</button>
    </form>
</div>
""")

@app.route('/send-code', methods=['POST'])
async def send_code():
    global cfg
    f = await request.form
    phone = f['phone'].strip()
    cfg['phone'] = phone
    save_config(cfg)
    
    tg = get_client()
    if not tg: return "Error: Client not initialized. Reset app."
    
    try:
        if not tg.is_connected(): await tg.connect()
        await tg.send_code_request(phone)
        return redirect('/telegram-login')
    except Exception as e:
        return f"Error: {e} <a href='/enter-phone'>Try Again</a>"

@app.route('/telegram-login')
async def telegram_login_page():
    return await render_template_string(STYLE + f"""
<div class="glass-container">
    <h3 style="text-align:center">💬 Verify OTP</h3>
    <p style="text-align:center; color:var(--text-sub)">Code sent to {cfg.get('phone')}</p>
    <form action="/verify-code" method="post">
        <input name="code" type="number" class="input" placeholder="12345" required>
        <button class="btn">Start Tracking</button>
    </form>
</div>
""")

@app.route('/verify-code', methods=['POST'])
async def verify_code():
    f = await request.form
    code = f['code']
    tg = get_client()
    try:
        if not tg.is_connected(): await tg.connect()
        await tg.sign_in(phone=cfg['phone'], code=code)
        return redirect('/')
    except Exception as e:
        return f"Invalid Code: {e} <a href='/telegram-login'>Try Again</a>"

# --- DASHBOARD & FEATURES ---
@app.route('/')
async def home():
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM targets') as c: rows = await c.fetchall()
    
    cards = ""
    for r in rows:
        status_cls = "online-badge" if r['current_status'] == 'online' else "offline-badge"
        dot = "<div class='dot'></div>" if r['current_status'] == 'online' else ""
        pic = f"/static/profile_pics/{r['pic_path']}" if r['pic_path'] else "https://ui-avatars.com/api/?name="+r['display_name']
        
        cards += f"""
        <a href="/target/{r['user_id']}">
            <div class="card">
                <div style="display:flex; align-items:center">
                    <img src="{pic}" class="avatar {'online' if r['current_status'] == 'online' else ''}">
                    <div>
                        <div style="font-weight:600; font-size:1rem">{r['display_name']}</div>
                        <div style="font-size:0.8rem; color:var(--text-sub)">{r['last_seen']}</div>
                    </div>
                </div>
                <div class="status-badge {status_cls}">{dot} {r['current_status']}</div>
            </div>
        </a>
        """
        
    return await render_template_string(STYLE + f"""
<div class="nav">
    <div style="font-weight:700; font-size:1.2rem"><i class="fas fa-radar"></i> ProTracker</div>
    <a href="/profile" style="color:var(--text-sub)"><i class="fas fa-cog" style="font-size:1.2rem"></i></a>
</div>

<div class="grid">
    {cards if cards else "<div style='text-align:center; color:var(--text-sub); grid-column:1/-1; padding:20px'>No targets active. Tap + to add one.</div>"}
</div>

<a href="/add" class="fab"><i class="fas fa-plus"></i></a>
""")

@app.route('/target/<int:uid>')
async def target_detail(uid):
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM targets WHERE user_id = ?', (uid,)) as c: target = await c.fetchone()
    
    if not target: return redirect('/')
    
    chart_data = await get_hourly_data(uid)
    insight = await get_ai_insight(uid)
    pic = f"/static/profile_pics/{target['pic_path']}" if target['pic_path'] else "https://ui-avatars.com/api/?name="+target['display_name']

    return await render_template_string(STYLE + f"""
<div class="nav">
    <a href="/"><i class="fas fa-arrow-left"></i></a>
    <div style="font-weight:600">{target['display_name']}</div>
    <a href="/delete/{uid}" onclick="return confirm('Stop tracking?')" style="color:#ef4444"><i class="fas fa-trash"></i></a>
</div>

<div class="grid" style="margin-top:0">
    <div style="grid-column: 1/-1; display:flex; flex-direction:column; align-items:center; padding:20px 0">
        <img src="{pic}" style="width:100px; height:100px; border-radius:50%; border:4px solid var(--card-bg); margin-bottom:10px">
        <div style="font-size:1.2rem; font-weight:700">{target['display_name']}</div>
        <div style="color:var(--text-sub)">@{target['username']}</div>
        <div class="status-badge {'online-badge' if target['current_status']=='online' else 'offline-badge'}" style="margin-top:10px">
            {target['current_status'].upper()}
        </div>
    </div>

    <div class="card" style="flex-direction:column; align-items:flex-start">
        <div style="font-weight:600; margin-bottom:10px"><i class="fas fa-robot"></i> AI Insight</div>
        <div style="color:var(--primary); font-size:0.9rem">{insight}</div>
    </div>

    <div class="card" style="grid-column: 1/-1; height:250px; display:block">
        <div style="font-weight:600; margin-bottom:10px">Activity Chart</div>
        <canvas id="chart"></canvas>
    </div>
    
    <a href="/export/{uid}" class="btn" style="text-align:center; display:block"><i class="fas fa-download"></i> Download Logs (CSV)</a>
</div>

<script>
new Chart(document.getElementById('chart'), {{
    type: 'bar',
    data: {{
        labels: Array.from({{length:24}},(_,i)=>i+':00'),
        datasets: [{{ label: 'Sessions', data: {chart_data}, backgroundColor: '#3b82f6', borderRadius: 4 }}]
    }},
    options: {{ responsive: true, maintainAspectRatio: false, scales: {{ x: {{ display: false }}, y: {{ beginAtZero: true, grid: {{ color: '#334155' }} }} }} }}
}});
</script>
""")

@app.route('/add', methods=['GET', 'POST'])
async def add():
    if request.method == 'GET':
        return await render_template_string(STYLE + """
<div class="glass-container">
    <h3>🎯 Add Target</h3>
    <form method="post">
        <label style="color:var(--text-sub); font-size:0.9rem">Username or Phone</label>
        <input name="target" class="input" placeholder="@username OR +9198..." required>
        <label style="color:var(--text-sub); font-size:0.9rem">Display Name</label>
        <input name="name" class="input" placeholder="e.g. Boss">
        <button class="btn">Start Tracking</button>
    </form>
    <a href="/" style="display:block; text-align:center; margin-top:20px; color:var(--text-sub)">Cancel</a>
</div>
""")
    
    # POST
    f = await request.form
    inp = f['target'].strip()
    name = f['name'] or inp
    tg = get_client()
    
    try:
        if inp.startswith('+') or inp.replace(' ','').isdigit():
            # Phone Logic
            contact = InputPhoneContact(client_id=0, phone=inp, first_name=name, last_name="")
            result = await tg(ImportContactsRequest([contact]))
            if result.users:
                e = result.users[0]
            else:
                return "Error: Number not on Telegram. <a href='/add'>Try Again</a>"
        else:
            # Username Logic
            e = await tg.get_entity(inp)
            
        pic = await download_pic(e, tg)
        
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute('INSERT OR IGNORE INTO targets (user_id, username, display_name, current_status, last_seen, pic_path) VALUES (?, ?, ?, ?, ?, ?)', 
                             (e.id, getattr(e,'username',''), name, 'CHECKING...', 'Just Now', pic))
            await db.commit()
        return redirect('/')
    except Exception as e:
        return f"Error: {e} <a href='/add'>Back</a>"

@app.route('/delete/<int:uid>')
async def delete(uid):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('DELETE FROM targets WHERE user_id = ?', (uid,))
        await db.commit()
    return redirect('/')

@app.route('/export/<int:uid>')
async def export(uid):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute('SELECT * FROM sessions WHERE user_id = ? ORDER BY id DESC', (uid,)) as c:
            rows = await c.fetchall()
    
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['ID', 'User ID', 'Status', 'Start', 'End', 'Duration'])
    cw.writerows(rows)
    return Response(si.getvalue(), mimetype='text/csv', headers={"Content-Disposition": f"attachment; filename=logs_{uid}.csv"})

@app.route('/reset')
async def reset():
    return await render_template_string(STYLE + """
<div class="glass-container">
    <h3>⚠️ Factory Reset</h3>
    <p>This will wipe all settings.</p>
    <form action="/do_reset" method="post">
        <button class="btn" style="background:#ef4444">Confirm Reset</button>
    </form>
    <a href="/login" style="display:block; text-align:center; margin-top:20px">Cancel</a>
</div>
""")

@app.route('/do_reset', methods=['POST'])
async def do_reset():
    if os.path.exists(CONFIG_FILE): os.remove(CONFIG_FILE)
    if os.path.exists(SESSION_FILE + '.session'): os.remove(SESSION_FILE + '.session')
    global cfg
    cfg = DEFAULT_CONFIG.copy()
    os.execv(sys.executable, ['python'] + sys.argv)

@app.route('/profile')
async def profile():
    return await render_template_string(STYLE + f"""
<div class="nav"><a href="/"><i class="fas fa-arrow-left"></i></a> <b>Settings</b> <div></div></div>
<div class="glass-container">
    <div style="word-break:break-all; background:rgba(34,197,94,0.1); padding:10px; border-radius:8px; border:1px solid #22c55e; color:#4ade80; margin-bottom:20px">
        <b>Recovery Key:</b><br>{cfg['recovery_key']}
    </div>
    <form action="/update_profile" method="post">
        <label>Update API ID</label>
        <input name="api_id" class="input" value="{cfg['api_id']}">
        <label>Update Username</label>
        <input name="username" class="input" value="{cfg['admin_username']}">
        <button class="btn">Save & Restart</button>
    </form>
    <a href="/logout" style="display:block; text-align:center; margin-top:20px; color:#ef4444">Logout</a>
</div>
""")

@app.route('/update_profile', methods=['POST'])
async def update_profile():
    f = await request.form
    global cfg
    cfg['api_id'] = int(f['api_id'])
    cfg['admin_username'] = f['username']
    save_config(cfg)
    os.execv(sys.executable, ['python'] + sys.argv)

@app.route('/logout')
async def logout():
    session.clear()
    return redirect('/login')

# --- MIDDLEWARE ---
@app.before_request
async def guard():
    if request.path.startswith('/static'): return
    if request.path in ('/setup', '/do_setup', '/login', '/do_login', '/reset', '/do_reset'): return
    
    if not cfg['is_setup_done']: return redirect('/setup')
    if 'user' not in session: return redirect('/login')
    
    if request.path in ('/enter-phone', '/send-code', '/telegram-login', '/verify-code'): return
    
    # Check TG Connection for Dashboard
    tg = get_client()
    if not tg: return redirect('/setup')
    
    if not tg.is_connected():
        try: await tg.connect()
        except: pass
        
    if not await tg.is_user_authorized():
        return redirect('/enter-phone')

@app.before_serving
async def start():
    print(f"KEY: {cfg['recovery_key']}")
    await init_db()
    app.add_background_task(tracker_loop)

if __name__ == '__main__':
    from hypercorn.config import Config
    import hypercorn.asyncio
    c = Config()
    c.bind = [f"0.0.0.0:{os.environ.get('PORT', 10000)}"]
    asyncio.run(hypercorn.asyncio.serve(app, c))

