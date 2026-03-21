import asyncio
import logging
import os
import hashlib
import secrets
import html as html_mod
import json
import io
import csv
import time
from functools import wraps
from datetime import datetime, timedelta
import aiosqlite
import pytz
from quart import Quart, render_template_string, request, redirect, url_for, session, abort, Response, jsonify
from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession
from telethon.tl.types import UserStatusOnline, UserStatusRecently, UserStatusOffline
from hypercorn.config import Config
from hypercorn.asyncio import serve

# --- CONFIGURATION ---
DEFAULT_API_ID   = os.getenv("API_ID", "")
DEFAULT_API_HASH = os.getenv("API_HASH", "")
ADMIN_USER  = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS  = os.getenv("ADMIN_PASS", "admin123")
SECRET_KEY  = os.getenv("SECRET_KEY", secrets.token_hex(32))
TIMEZONE    = 'Asia/Kolkata'
TZ          = pytz.timezone(TIMEZONE)

# Rate-limit store: { ip: [timestamp, ...] }
_login_attempts: dict = {}
LOGIN_MAX     = 10        # max attempts
LOGIN_WINDOW  = 60        # seconds

# Async lock protecting global API credential mutations
_api_lock = asyncio.Lock()

# --- PASSWORD HELPERS ---
def hash_password(password: str) -> str:
    """Return a salted PBKDF2-SHA256 hash."""
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100_000)
    return f"{salt}:{h.hex()}"

def verify_password(password: str, stored: str) -> bool:
    """Verify against a hashed password, with plain-text fallback for legacy rows."""
    try:
        salt, hashed = stored.split(':', 1)
        h = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100_000)
        return h.hex() == hashed
    except ValueError:
        return password == stored  # legacy plain-text row

# --- XSS ESCAPING ---
def e(val) -> str:
    """HTML-escape any user-supplied value before interpolating into templates."""
    if val is None: return ""
    return html_mod.escape(str(val))

# --- CSRF ---
def generate_csrf() -> str:
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_hex(24)
    return session['_csrf']

def validate_csrf(token: str) -> bool:
    return token and session.get('_csrf') == token

CSRF_INPUT = '<input type="hidden" name="_csrf" value="{token}">'

# --- RATE LIMITING ---
def check_rate_limit(ip: str) -> bool:
    """Return True (allow) if under limit, False (block) if over."""
    now = time.time()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < LOGIN_WINDOW]
    _login_attempts[ip] = attempts
    if len(attempts) >= LOGIN_MAX:
        return False
    _login_attempts[ip].append(now)
    return True


logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("Spectre.Flawless")

# --- APP SETUP ---
app = Quart(__name__)
app.secret_key = SECRET_KEY

# Global vars
temp_client = None
phone_number = None
phone_code_hash = None
runtime_api_id = int(DEFAULT_API_ID) if DEFAULT_API_ID.isdigit() else 0
runtime_api_hash = DEFAULT_API_HASH

# --- DATABASE SCHEMA ---
DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE,
    password TEXT,
    is_admin BOOLEAN DEFAULT 0,
    status TEXT DEFAULT 'active',
    max_targets INTEGER DEFAULT 3,
    expiry_date DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER DEFAULT 1,
    tg_id INTEGER,
    tg_username TEXT,
    phone TEXT,
    display_name TEXT,
    last_status TEXT,
    last_seen DATETIME,
    is_tracking BOOLEAN DEFAULT 1,
    pic_path TEXT,
    notes TEXT,
    notifications BOOLEAN DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER,
    status TEXT,
    start_time DATETIME,
    end_time DATETIME,
    duration TEXT,
    FOREIGN KEY(target_id) REFERENCES targets(id)
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER,
    event_type TEXT,
    timestamp DATETIME,
    metadata TEXT
);
"""

# --- UI TEMPLATE ---
HTML_BASE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>SPECTRE</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;600;700&family=Lora:ital,wght@0,400;0,500;0,600;1,400&family=Cormorant+Garamond:ital,wght@0,400;0,600;1,400&display=swap" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        /* ═══════════════════════════════════════════
           DESIGN TOKENS
        ═══════════════════════════════════════════ */
        :root {
            --bg:          #F2EFE8;
            --bg-2:        #EBE7DF;
            --sidebar-bg:  #141F18;
            --sidebar-2:   #1C2D22;
            --surface:     #FFFFFF;
            --surface-2:   #F9F7F2;
            --text:        #1A1A1A;
            --text-mid:    #444;
            --text-muted:  #7A7268;
            --accent:      #1A2F23;
            --gold:        #C09A50;
            --gold-dim:    rgba(192,154,80,0.18);
            --gold-line:   rgba(192,154,80,0.35);
            --border:      #DDD9CE;
            --border-soft: #EAE6DC;
            --success:     #2A5224;
            --success-bg:  rgba(42,82,36,0.07);
            --warn:        #7A5500;
            --warn-bg:     #FFF8E6;
            --danger:      #7A1515;
            --danger-bg:   #FFF0F0;
            --recently:    #7A5C00;
            --recently-bg: rgba(122,92,0,0.07);
            --shadow-sm:   0 1px 3px rgba(0,0,0,0.06);
            --shadow-md:   0 4px 16px rgba(26,47,35,0.09);
            --shadow-lg:   0 12px 40px rgba(26,47,35,0.13);
            --sidebar-w:   256px;
            --sidebar-collapsed: 64px;
            --radius:      2px;
            --transition:  0.18s ease;
        }

        /* ═══════════════════════════════════════════
           RESET & BASE
        ═══════════════════════════════════════════ */
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        html { scroll-behavior: smooth; }
        body {
            background: var(--bg);
            color: var(--text);
            font-family: 'Lora', Georgia, serif;
            font-size: 15px;
            line-height: 1.6;
            display: flex;
            min-height: 100vh;
            overflow-x: hidden;
            -webkit-font-smoothing: antialiased;
        }

        /* Custom scrollbar */
        ::-webkit-scrollbar { width: 5px; height: 5px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 10px; }
        ::-webkit-scrollbar-thumb:hover { background: var(--gold); }

        /* Page fade-in */
        @keyframes fadeUp {
            from { opacity: 0; transform: translateY(10px); }
            to   { opacity: 1; transform: translateY(0); }
        }
        .main { animation: fadeUp 0.3s ease both; }

        /* ═══════════════════════════════════════════
           SIDEBAR
        ═══════════════════════════════════════════ */
        .sidebar {
            width: var(--sidebar-w);
            background: var(--sidebar-bg);
            display: flex;
            flex-direction: column;
            padding: 0;
            position: fixed;
            height: 100vh;
            z-index: 100;
            overflow: hidden;
            transition: width var(--transition);
            border-right: 1px solid rgba(192,154,80,0.2);
        }

        .sidebar-top {
            padding: 1.8rem 1.4rem 1.4rem;
            border-bottom: 1px solid rgba(255,255,255,0.04);
        }

        .brand {
            font-family: 'Cinzel', serif;
            font-size: 1.15rem;
            font-weight: 700;
            color: var(--gold);
            text-decoration: none;
            letter-spacing: 0.18em;
            display: flex;
            align-items: center;
            gap: 12px;
            white-space: nowrap;
        }
        .brand-icon {
            width: 34px; height: 34px;
            background: var(--gold-dim);
            border: 1px solid var(--gold-line);
            border-radius: var(--radius);
            display: flex; align-items: center; justify-content: center;
            flex-shrink: 0;
            color: var(--gold);
            font-size: 0.95rem;
        }
        .brand-text { white-space: nowrap; overflow: hidden; }
        .brand-sub {
            display: block;
            font-family: 'Lora', serif;
            font-size: 0.52rem;
            font-weight: 400;
            color: rgba(192,154,80,0.55);
            letter-spacing: 0.22em;
            font-style: italic;
            margin-top: 1px;
        }

        .menu {
            flex: 1;
            padding: 1.2rem 0.8rem;
            display: flex;
            flex-direction: column;
            gap: 2px;
            overflow-y: auto;
        }
        .menu-label {
            font-family: 'Cinzel', serif;
            font-size: 0.52rem;
            color: rgba(224,216,200,0.3);
            text-transform: uppercase;
            letter-spacing: 0.25em;
            padding: 0.9rem 0.6rem 0.3rem;
            white-space: nowrap;
            overflow: hidden;
        }
        .nav-item {
            padding: 0.7rem 0.8rem;
            color: rgba(224,216,200,0.65);
            text-decoration: none;
            font-size: 0.75rem;
            font-family: 'Cinzel', serif;
            letter-spacing: 0.08em;
            display: flex;
            align-items: center;
            gap: 12px;
            border-radius: var(--radius);
            transition: all var(--transition);
            position: relative;
            white-space: nowrap;
            overflow: hidden;
        }
        .nav-item:hover {
            background: rgba(192,154,80,0.08);
            color: rgba(224,216,200,0.9);
        }
        .nav-item.active {
            background: rgba(192,154,80,0.12);
            color: var(--gold);
        }
        .nav-item.active::before {
            content: '';
            position: absolute;
            left: 0; top: 20%; bottom: 20%;
            width: 2px;
            background: var(--gold);
            border-radius: 0 2px 2px 0;
        }
        .nav-item i {
            width: 18px;
            text-align: center;
            font-size: 0.85rem;
            flex-shrink: 0;
            opacity: 0.8;
        }
        .nav-item.active i { opacity: 1; }

        .sidebar-footer {
            padding: 1rem 1.2rem;
            border-top: 1px solid rgba(192,154,80,0.12);
            display: flex;
            align-items: center;
            gap: 10px;
            white-space: nowrap;
            overflow: hidden;
        }
        .avatar {
            width: 32px; height: 32px;
            background: var(--gold-dim);
            border: 1px solid var(--gold-line);
            border-radius: 50%;
            display: flex; align-items: center; justify-content: center;
            font-family: 'Cinzel', serif;
            font-weight: 700;
            font-size: 0.75rem;
            color: var(--gold);
            flex-shrink: 0;
        }
        .user-info { overflow: hidden; flex: 1; }
        .user-name {
            font-family: 'Cinzel', serif;
            font-size: 0.65rem;
            color: rgba(224,216,200,0.75);
            letter-spacing: 0.08em;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .user-role {
            font-size: 0.55rem;
            color: rgba(192,154,80,0.5);
            font-style: italic;
        }
        .logout-btn {
            color: rgba(192,154,80,0.5);
            text-decoration: none;
            font-size: 0.8rem;
            padding: 4px;
            transition: color var(--transition);
            flex-shrink: 0;
        }
        .logout-btn:hover { color: var(--gold); }

        /* ═══════════════════════════════════════════
           MAIN CONTENT
        ═══════════════════════════════════════════ */
        .main {
            flex: 1;
            margin-left: var(--sidebar-w);
            padding: 2.8rem 3.5rem;
            min-width: 0;
            width: calc(100% - var(--sidebar-w));
        }

        /* ═══════════════════════════════════════════
           TYPOGRAPHY
        ═══════════════════════════════════════════ */
        h1 {
            font-family: 'Cinzel', serif;
            font-size: 1.75rem;
            font-weight: 700;
            color: var(--accent);
            letter-spacing: 0.04em;
            line-height: 1.15;
            margin-bottom: 0.3rem;
        }
        .page-eyebrow {
            font-family: 'Cinzel', serif;
            font-size: 0.58rem;
            color: var(--gold);
            letter-spacing: 0.25em;
            text-transform: uppercase;
            margin-bottom: 0.4rem;
        }
        .subtitle {
            color: var(--text-muted);
            font-size: 0.85rem;
            font-style: italic;
            margin-bottom: 2rem;
            padding-left: 0.9rem;
            border-left: 2px solid var(--gold-line);
        }
        .section-title {
            font-family: 'Cinzel', serif;
            font-size: 0.62rem;
            letter-spacing: 0.18em;
            color: var(--text-mid);
            text-transform: uppercase;
        }

        /* ═══════════════════════════════════════════
           HEADER ROW
        ═══════════════════════════════════════════ */
        .page-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 2rem;
            gap: 1rem;
            flex-wrap: wrap;
        }
        .page-header-actions {
            display: flex;
            gap: 8px;
            align-items: center;
            flex-wrap: wrap;
            padding-top: 4px;
        }

        /* ═══════════════════════════════════════════
           STAT CARDS
        ═══════════════════════════════════════════ */
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1.2rem;
            margin-bottom: 2rem;
        }
        .stat-card {
            background: var(--surface);
            border: 1px solid var(--border);
            padding: 1.4rem 1.6rem;
            position: relative;
            overflow: hidden;
            transition: box-shadow var(--transition), transform var(--transition);
        }
        .stat-card:hover {
            box-shadow: var(--shadow-md);
            transform: translateY(-1px);
        }
        .stat-card::before {
            content: '';
            position: absolute;
            top: 0; left: 0; right: 0;
            height: 2px;
            background: linear-gradient(90deg, var(--accent), transparent);
        }
        .stat-card-icon {
            position: absolute;
            right: 1.2rem; bottom: 1rem;
            font-size: 2.5rem;
            color: var(--border);
            pointer-events: none;
        }
        .stat-label {
            font-family: 'Cinzel', serif;
            font-size: 0.58rem;
            letter-spacing: 0.18em;
            text-transform: uppercase;
            color: var(--text-muted);
        }
        .stat-val {
            font-family: 'Cormorant Garamond', serif;
            font-size: 2.4rem;
            font-weight: 600;
            color: var(--accent);
            line-height: 1.1;
            margin: 0.3rem 0 0.2rem;
        }
        .stat-sub {
            font-size: 0.72rem;
            color: var(--text-muted);
            font-style: italic;
        }

        /* ═══════════════════════════════════════════
           SURFACE PANEL (card/table wrapper)
        ═══════════════════════════════════════════ */
        .panel {
            background: var(--surface);
            border: 1px solid var(--border);
            box-shadow: var(--shadow-sm);
            margin-bottom: 1.5rem;
        }
        .panel-header {
            padding: 1.1rem 1.5rem;
            border-bottom: 1px solid var(--border-soft);
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 0.8rem;
        }
        .panel-body { padding: 1.5rem; }

        /* ═══════════════════════════════════════════
           TABLE
        ═══════════════════════════════════════════ */
        .table-wrap { overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; min-width: 460px; }
        thead tr { background: var(--surface-2); }
        th {
            text-align: left;
            padding: 0.85rem 1.2rem;
            font-family: 'Cinzel', serif;
            font-size: 0.58rem;
            letter-spacing: 0.16em;
            color: var(--text-muted);
            border-bottom: 1px solid var(--border);
            white-space: nowrap;
        }
        td {
            padding: 0.95rem 1.2rem;
            border-bottom: 1px solid var(--border-soft);
            font-size: 0.88rem;
            vertical-align: middle;
        }
        tbody tr {
            cursor: default;
            transition: background var(--transition);
        }
        tbody tr:hover td { background: var(--surface-2); }
        tbody tr:last-child td { border-bottom: none; }
        .clickable-row { cursor: pointer; }

        /* ═══════════════════════════════════════════
           STATUS BADGES
        ═══════════════════════════════════════════ */
        .badge {
            display: inline-flex;
            align-items: center;
            gap: 5px;
            padding: 3px 9px 3px 7px;
            font-family: 'Cinzel', serif;
            font-size: 0.58rem;
            letter-spacing: 0.1em;
            text-transform: uppercase;
            border-radius: 2px;
            border: 1px solid;
            white-space: nowrap;
        }
        .badge-dot {
            width: 5px; height: 5px;
            border-radius: 50%;
            display: inline-block;
            flex-shrink: 0;
        }
        .badge-online {
            color: var(--success);
            border-color: rgba(42,82,36,0.3);
            background: var(--success-bg);
        }
        .badge-online .badge-dot {
            background: var(--success);
            box-shadow: 0 0 0 0 rgba(42,82,36,0.4);
            animation: pulse-green 2s infinite;
        }
        .badge-offline {
            color: var(--text-muted);
            border-color: var(--border);
            background: transparent;
        }
        .badge-offline .badge-dot { background: #ccc; }
        .badge-recently {
            color: var(--recently);
            border-color: rgba(122,92,0,0.25);
            background: var(--recently-bg);
        }
        .badge-recently .badge-dot { background: var(--recently); }
        .badge-unknown {
            color: var(--text-muted);
            border-color: var(--border);
            background: transparent;
        }
        .badge-unknown .badge-dot { background: #ccc; }

        @keyframes pulse-green {
            0%   { box-shadow: 0 0 0 0 rgba(42,82,36,0.5); }
            70%  { box-shadow: 0 0 0 5px rgba(42,82,36,0); }
            100% { box-shadow: 0 0 0 0 rgba(42,82,36,0); }
        }

        /* ═══════════════════════════════════════════
           FORMS
        ═══════════════════════════════════════════ */
        .form-group { margin-bottom: 1.3rem; }
        label {
            display: block;
            font-family: 'Cinzel', serif;
            font-size: 0.58rem;
            letter-spacing: 0.16em;
            color: var(--text-muted);
            margin-bottom: 0.5rem;
            text-transform: uppercase;
        }
        input, select, textarea {
            width: 100%;
            padding: 11px 13px;
            border: 1px solid var(--border);
            background: var(--surface-2);
            font-family: 'Lora', serif;
            font-size: 0.9rem;
            color: var(--text);
            border-radius: var(--radius);
            transition: border-color var(--transition), box-shadow var(--transition);
            -webkit-appearance: none;
            appearance: none;
        }
        input:focus, select:focus, textarea:focus {
            border-color: var(--gold);
            box-shadow: 0 0 0 3px rgba(192,154,80,0.12);
            outline: none;
            background: var(--surface);
        }
        textarea { resize: vertical; min-height: 100px; }
        /* Autofill override */
        input:-webkit-autofill,
        input:-webkit-autofill:focus {
            -webkit-box-shadow: 0 0 0 1000px var(--surface-2) inset !important;
            -webkit-text-fill-color: var(--text) !important;
        }
        select {
            background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%237A7268'/%3E%3C/svg%3E");
            background-repeat: no-repeat;
            background-position: right 12px center;
            padding-right: 32px;
        }

        /* ═══════════════════════════════════════════
           BUTTONS
        ═══════════════════════════════════════════ */
        .btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            padding: 10px 20px;
            font-family: 'Cinzel', serif;
            font-size: 0.68rem;
            font-weight: 700;
            letter-spacing: 0.12em;
            text-transform: uppercase;
            text-decoration: none;
            cursor: pointer;
            transition: all var(--transition);
            border-radius: var(--radius);
            white-space: nowrap;
            border: 1px solid transparent;
        }
        .btn-primary {
            background: var(--accent);
            color: var(--gold);
            border-color: var(--accent);
        }
        .btn-primary:hover {
            background: #253d30;
            box-shadow: 0 4px 12px rgba(26,47,35,0.25);
            transform: translateY(-1px);
        }
        .btn-primary:active { transform: translateY(0); box-shadow: none; }

        .btn-outline {
            background: transparent;
            color: var(--text-mid);
            border-color: var(--border);
        }
        .btn-outline:hover {
            border-color: var(--text-muted);
            color: var(--text);
            background: var(--surface-2);
        }

        .btn-danger {
            background: transparent;
            color: var(--danger);
            border-color: rgba(122,21,21,0.25);
        }
        .btn-danger:hover {
            background: var(--danger-bg);
            border-color: var(--danger);
        }

        .btn-full { width: 100%; }
        .btn-sm { padding: 6px 14px; font-size: 0.6rem; }

        /* ═══════════════════════════════════════════
           DIVIDER / SEPARATOR
        ═══════════════════════════════════════════ */
        .divider {
            border: none;
            border-top: 1px solid var(--border-soft);
            margin: 1.5rem 0;
        }
        .divider-label {
            text-align: center;
            position: relative;
            font-family: 'Cinzel', serif;
            font-size: 0.55rem;
            letter-spacing: 0.2em;
            color: var(--text-muted);
            margin: 1.5rem 0;
        }
        .divider-label::before, .divider-label::after {
            content: '';
            position: absolute;
            top: 50%;
            width: 40%;
            height: 1px;
            background: var(--border-soft);
        }
        .divider-label::before { left: 0; }
        .divider-label::after  { right: 0; }

        /* ═══════════════════════════════════════════
           ALERT BANNERS
        ═══════════════════════════════════════════ */
        .alert {
            display: flex;
            align-items: flex-start;
            gap: 10px;
            padding: 0.8rem 1.1rem;
            border-radius: var(--radius);
            font-size: 0.8rem;
            margin-bottom: 1.2rem;
            border: 1px solid;
        }
        .alert i { margin-top: 1px; flex-shrink: 0; }
        .alert-warn  { background: var(--warn-bg);   border-color: rgba(122,85,0,0.25);  color: var(--warn);   }
        .alert-error { background: var(--danger-bg); border-color: rgba(122,21,21,0.25); color: var(--danger); }
        .alert-ok    { background: var(--success-bg);border-color: rgba(42,82,36,0.25);  color: var(--success);}

        /* ═══════════════════════════════════════════
           TOAST NOTIFICATION
        ═══════════════════════════════════════════ */
        #toast {
            position: fixed;
            bottom: 2rem; right: 2rem;
            background: var(--accent);
            color: var(--gold);
            font-family: 'Cinzel', serif;
            font-size: 0.68rem;
            letter-spacing: 0.12em;
            padding: 0.9rem 1.4rem;
            border: 1px solid var(--gold-line);
            box-shadow: var(--shadow-lg);
            z-index: 999;
            transform: translateY(10px);
            opacity: 0;
            transition: all 0.3s ease;
            pointer-events: none;
            max-width: 320px;
        }
        #toast.show { transform: translateY(0); opacity: 1; }

        /* ═══════════════════════════════════════════
           SEARCH BAR
        ═══════════════════════════════════════════ */
        .search-wrap {
            position: relative;
            display: flex;
            align-items: center;
        }
        .search-wrap i {
            position: absolute;
            left: 10px;
            color: var(--gold);
            font-size: 0.75rem;
            pointer-events: none;
        }
        .search-wrap input {
            padding-left: 2.2rem;
            padding-top: 8px;
            padding-bottom: 8px;
            font-size: 0.8rem;
            border-color: var(--border-soft);
            background: var(--surface-2);
            min-width: 200px;
        }

        /* ═══════════════════════════════════════════
           IDENTITY CELL (name + id sub)
        ═══════════════════════════════════════════ */
        .identity-name {
            font-family: 'Cormorant Garamond', serif;
            font-size: 1.05rem;
            font-weight: 600;
            color: var(--text);
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .identity-id {
            font-size: 0.75rem;
            color: var(--text-muted);
            font-family: 'Courier New', monospace;
            margin-top: 1px;
        }

        /* ═══════════════════════════════════════════
           EMPTY STATE
        ═══════════════════════════════════════════ */
        .empty-state {
            text-align: center;
            padding: 4rem 2rem;
            color: var(--text-muted);
        }
        .empty-state-icon {
            font-size: 2.5rem;
            color: var(--border);
            margin-bottom: 1rem;
        }
        .empty-state-title {
            font-family: 'Cinzel', serif;
            font-size: 0.85rem;
            letter-spacing: 0.1em;
            color: var(--text-mid);
            margin-bottom: 0.4rem;
        }
        .empty-state-sub {
            font-size: 0.8rem;
            font-style: italic;
        }

        /* ═══════════════════════════════════════════
           LOGIN PAGE
        ═══════════════════════════════════════════ */
        .login-bg {
            min-height: 100vh;
            width: 100%;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 1.5rem;
            background: var(--bg);
            background-image:
                linear-gradient(rgba(26,47,35,0.03) 1px, transparent 1px),
                linear-gradient(90deg, rgba(26,47,35,0.03) 1px, transparent 1px);
            background-size: 40px 40px;
        }
        .login-card {
            width: 100%;
            max-width: 400px;
            background: var(--surface);
            border: 1px solid var(--gold-line);
            box-shadow: var(--shadow-lg);
            padding: 3rem;
            position: relative;
        }
        .login-card::before {
            content: '';
            position: absolute;
            inset: 5px;
            border: 1px solid var(--border-soft);
            pointer-events: none;
        }
        .login-logo {
            text-align: center;
            margin-bottom: 2.2rem;
        }
        .login-logo-icon {
            width: 52px; height: 52px;
            background: var(--accent);
            border: 1px solid var(--gold-line);
            border-radius: var(--radius);
            display: inline-flex;
            align-items: center;
            justify-content: center;
            color: var(--gold);
            font-size: 1.4rem;
            margin-bottom: 1rem;
        }
        .login-title {
            font-family: 'Cinzel', serif;
            font-size: 1.6rem;
            letter-spacing: 0.2em;
            color: var(--accent);
        }
        .login-sub {
            font-family: 'Lora', serif;
            font-size: 0.78rem;
            color: var(--text-muted);
            font-style: italic;
            margin-top: 0.3rem;
        }

        /* ═══════════════════════════════════════════
           MOBILE — bottom nav
        ═══════════════════════════════════════════ */
        .mobile-nav { display: none; }

        @media (max-width: 860px) {
            .sidebar { display: none; }
            .main { margin-left: 0; padding: 1.4rem 1rem 5rem; width: 100%; }
            .mobile-nav {
                display: flex;
                position: fixed;
                bottom: 0; left: 0; right: 0;
                background: var(--sidebar-bg);
                border-top: 1px solid rgba(192,154,80,0.2);
                z-index: 100;
                height: 58px;
                padding: 0 0.5rem;
            }
            .mobile-nav a {
                flex: 1;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                color: rgba(224,216,200,0.45);
                text-decoration: none;
                font-size: 1.1rem;
                gap: 3px;
                transition: color var(--transition);
                position: relative;
            }
            .mobile-nav a span {
                font-family: 'Cinzel', serif;
                font-size: 0.42rem;
                letter-spacing: 0.1em;
                text-transform: uppercase;
            }
            .mobile-nav a.active, .mobile-nav a:hover { color: var(--gold); }
            .mobile-nav a.active::after {
                content: '';
                position: absolute;
                top: 0; left: 20%; right: 20%;
                height: 2px;
                background: var(--gold);
                border-radius: 0 0 2px 2px;
            }
            h1 { font-size: 1.5rem; }
            .stats-grid { grid-template-columns: 1fr 1fr; gap: 0.8rem; }
            .stat-val { font-size: 1.9rem; }
            .page-header { flex-direction: column; gap: 0.8rem; }
            .login-card { padding: 2rem 1.5rem; }
        }
        @media (max-width: 480px) {
            .stats-grid { grid-template-columns: 1fr; }
        }

        /* ═══════════════════════════════════════════
           MISC UTILS
        ═══════════════════════════════════════════ */
        .mono { font-family: 'Courier New', monospace; }
        .text-gold { color: var(--gold); }
        .text-muted { color: var(--text-muted); }
        .text-success { color: var(--success); }
        .text-danger { color: var(--danger); }
        .fw-600 { font-weight: 600; }
        .mt-1 { margin-top: 0.5rem; }
        .mt-2 { margin-top: 1rem; }
        .gap-sm { gap: 0.5rem; }
        .flex { display: flex; align-items: center; }
        .flex-between { display: flex; justify-content: space-between; align-items: center; }
    </style>
</head>
<body>

    {% if not hide_sidebar %}
    <!-- ── SIDEBAR ── -->
    <nav class="sidebar">
        <div class="sidebar-top">
            <a href="/dashboard" class="brand">
                <div class="brand-icon"><i class="fas fa-chess-knight"></i></div>
                <div class="brand-text">
                    SPECTRE
                    <span class="brand-sub">Intelligence Ledger</span>
                </div>
            </a>
        </div>

        <div class="menu">
            <div class="menu-label">Operations</div>
            <a href="/dashboard" class="nav-item {{ 'active' if active_page == 'dashboard' else '' }}">
                <i class="fas fa-table-cells-large"></i><span>Dashboard</span>
            </a>
            <a href="/intelligence" class="nav-item {{ 'active' if active_page == 'intelligence' else '' }}">
                <i class="fas fa-circle-nodes"></i><span>Intelligence</span>
            </a>
            <a href="/logs" class="nav-item {{ 'active' if active_page == 'logs' else '' }}">
                <i class="fas fa-clock-rotate-left"></i><span>Archive</span>
            </a>

            <div class="menu-label" style="margin-top:0.8rem;">System</div>
            <a href="/settings" class="nav-item {{ 'active' if active_page == 'settings' else '' }}">
                <i class="fas fa-sliders"></i><span>Config</span>
            </a>
            {% if session.get('is_admin') %}
            <a href="/users" class="nav-item {{ 'active' if active_page == 'users' else '' }}">
                <i class="fas fa-users-gear"></i><span>Members</span>
            </a>
            {% endif %}
        </div>

        <div class="sidebar-footer">
            <div class="avatar">{{ session.get('username', 'A')[0].upper() }}</div>
            <div class="user-info">
                <div class="user-name">{{ session.get('username', 'AGENT').upper() }}</div>
                <div class="user-role">{{ 'Administrator' if session.get('is_admin') else 'Operative' }}</div>
            </div>
            <a href="/logout" class="logout-btn" title="Sign out"><i class="fas fa-arrow-right-from-bracket"></i></a>
        </div>
    </nav>

    <!-- ── MOBILE BOTTOM NAV ── -->
    <nav class="mobile-nav">
        <a href="/dashboard" class="{{ 'active' if active_page == 'dashboard' else '' }}">
            <i class="fas fa-table-cells-large"></i><span>Dashboard</span>
        </a>
        <a href="/intelligence" class="{{ 'active' if active_page == 'intelligence' else '' }}">
            <i class="fas fa-circle-nodes"></i><span>Intel</span>
        </a>
        <a href="/logs" class="{{ 'active' if active_page == 'logs' else '' }}">
            <i class="fas fa-clock-rotate-left"></i><span>Archive</span>
        </a>
        <a href="/settings" class="{{ 'active' if active_page == 'settings' else '' }}">
            <i class="fas fa-sliders"></i><span>Config</span>
        </a>
        {% if session.get('is_admin') %}
        <a href="/users" class="{{ 'active' if active_page == 'users' else '' }}">
            <i class="fas fa-users-gear"></i><span>Members</span>
        </a>
        {% endif %}
    </nav>
    {% endif %}

    <!-- ── MAIN ── -->
    <main class="main">
        %%CONTENT%%
    </main>

    <!-- ── TOAST ── -->
    <div id="toast"></div>

    <script>
    // Toast helper
    function showToast(msg, duration) {
        const t = document.getElementById('toast');
        if (!t) return;
        t.textContent = msg;
        t.classList.add('show');
        setTimeout(() => t.classList.remove('show'), duration || 2800);
    }
    // Auto-show toast from URL param
    (function(){
        const p = new URLSearchParams(location.search);
        if (p.get('saved') === '1') showToast('✓ SAVED SUCCESSFULLY');
        if (p.get('err') === 'quota') showToast('✗ TARGET QUOTA REACHED', 3500);
    })();
    </script>
</body>
</html>
"""

# --- DATABASE ENGINE ---
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
        # --- AUTO REPAIR ---
        async def add_col(t, c, d):
            try: await db.execute(f"SELECT {c} FROM {t} LIMIT 1")
            except Exception:
                try: await db.execute(f"ALTER TABLE {t} ADD COLUMN {c} {d}")
                except Exception as ae: logger.debug(f"add_col {t}.{c}: {ae}")
        
        await add_col('targets', 'owner_id', 'INTEGER DEFAULT 1')
        await add_col('targets', 'tg_username', 'TEXT')
        await add_col('targets', 'phone', 'TEXT')
        await add_col('targets', 'pic_path', 'TEXT')
        await add_col('targets', 'notes', 'TEXT')
        await add_col('targets', 'notifications', 'BOOLEAN DEFAULT 0')
        await add_col('users', 'max_targets', 'INTEGER DEFAULT 3')
        await add_col('users', 'expiry_date', 'DATETIME')
        await add_col('users', 'created_at', 'DATETIME DEFAULT CURRENT_TIMESTAMP')
        await add_col('logs', 'metadata', 'TEXT')

        # --- INDEXES (idempotent) ---
        await db.executescript("""
            CREATE INDEX IF NOT EXISTS idx_sessions_target ON sessions(target_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_end    ON sessions(end_time);
            CREATE INDEX IF NOT EXISTS idx_logs_target     ON logs(target_id);
            CREATE INDEX IF NOT EXISTS idx_logs_ts         ON logs(timestamp);
            CREATE INDEX IF NOT EXISTS idx_targets_owner   ON targets(owner_id);
        """)
        await db.commit()

        # --- CLOSE ORPHAN SESSIONS from previous crash ---
        now_str = datetime.now(TZ).isoformat()
        await db.execute("""
            UPDATE sessions SET end_time=?, duration='unknown', status='CRASHED'
            WHERE end_time IS NULL
        """, (now_str,))
        await db.commit()
        
        async with db.execute("SELECT * FROM users WHERE username = ?", (ADMIN_USER,)) as c:
            if not await c.fetchone():
                await db.execute("INSERT INTO users (username, password, is_admin, max_targets) VALUES (?, ?, 1, 9999)", (ADMIN_USER, hash_password(ADMIN_PASS)))
                await db.commit()

    async with await get_db() as db:
        async with db.execute("SELECT value FROM settings WHERE key='api_id'") as c:
            r = await c.fetchone()
            if r: global runtime_api_id; runtime_api_id = int(r['value'])
        async with db.execute("SELECT value FROM settings WHERE key='api_hash'") as c:
            r = await c.fetchone()
            if r: global runtime_api_hash; runtime_api_hash = r['value']

async def get_setting(key):
    async with await get_db() as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as c:
            row = await c.fetchone()
            return row['value'] if row else None

async def save_setting(key, value):
    async with await get_db() as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        await db.commit()

# --- HELPERS ---
def now_tz(): return datetime.now(TZ)
def fmt_time(dt_obj):
    if not dt_obj: return "—"
    if isinstance(dt_obj, str):
        try: dt_obj = datetime.fromisoformat(dt_obj)
        except: return dt_obj
    return dt_obj.strftime('%d %b, %H:%M')

def calc_duration(start, end):
    if isinstance(start, str): start = datetime.fromisoformat(start)
    if isinstance(end, str): end = datetime.fromisoformat(end)
    diff = end - start
    ts = int(diff.total_seconds())
    h, r = divmod(ts, 3600); m, s = divmod(r, 60)
    return f"{h}h {m}m" if h > 0 else f"{m}m {s}s"

async def get_heatmap_data(target_id):
    h = [0] * 24
    async with await get_db() as db:
        async with db.execute("SELECT start_time, end_time FROM sessions WHERE target_id=? ORDER BY id DESC LIMIT 100", (target_id,)) as c:
            rows = await c.fetchall()
    for r in rows:
        try:
            s = datetime.fromisoformat(r['start_time']) if isinstance(r['start_time'], str) else r['start_time']
            e = datetime.fromisoformat(r['end_time']) if r['end_time'] else now_tz()
            h[s.hour] += 1
            while s.hour != e.hour: s += timedelta(hours=1); h[s.hour] += 1
        except Exception as he: logger.debug(f"heatmap row: {he}")

# --- ENGINE ---
class CyberTracker:
    def __init__(self):
        self.client = None
        self.tracking_active = False
        self._backoff = 5       # current sleep between cycles (seconds)
        _BACKOFF_MAX  = 300     # cap at 5 min when Telegram is angry

    async def start(self):
        async with await get_db() as db:
            async with db.execute("SELECT value FROM settings WHERE key='session_string'") as c:
                row = await c.fetchone()
                session_str = row['value'] if row else None

        if not runtime_api_id or not runtime_api_hash or not session_str: return
        try:
            self.client = TelegramClient(StringSession(session_str), runtime_api_id, runtime_api_hash)
            await self.client.connect()
            if not await self.client.is_user_authorized(): return
            self.tracking_active = True
            self._backoff = 5
            asyncio.create_task(self.loop())
        except Exception as e: logger.error(f"Engine start: {e}")

    async def stop(self):
        """Graceful shutdown — disconnect Telegram client cleanly."""
        self.tracking_active = False
        if self.client:
            try:
                await self.client.disconnect()
                logger.info("Telegram client disconnected cleanly.")
            except Exception as e:
                logger.warning(f"Client disconnect error: {e}")

    async def loop(self):
        _BACKOFF_MAX = 300
        while self.tracking_active:
            try:
                async with await get_db() as db:
                    async with db.execute("SELECT * FROM targets WHERE is_tracking = 1") as c:
                        targets = await c.fetchall()
                    for t in targets:
                        await self.probe_target(db, t)
                        await asyncio.sleep(0.5)
                self._backoff = 5  # reset on success
            except Exception as e:
                logger.error(f"Tracker loop error: {e}")
                self._backoff = min(self._backoff * 2, _BACKOFF_MAX)
                logger.info(f"Backing off for {self._backoff}s")
            await asyncio.sleep(self._backoff)

    async def probe_target(self, db, target):
        t_id = target['id']
        tg_val = None
        if target['tg_id'] and target['tg_id'] != 0: tg_val = target['tg_id']
        elif target['phone']: tg_val = target['phone']
        elif target['tg_username']: tg_val = target['tg_username']
        
        if not tg_val: return
        try:
            entity = await self.client.get_entity(tg_val)
            if (not target['tg_id'] or target['tg_id'] == 0) and entity.id:
                await db.execute("UPDATE targets SET tg_id=? WHERE id=?", (entity.id, t_id))
            
            status = 'offline'
            if isinstance(entity.status, UserStatusOnline): status = 'online'
            elif isinstance(entity.status, UserStatusRecently): status = 'recently'
            
            if status != target['last_status']:
                now = now_tz()
                await db.execute("UPDATE targets SET last_status=?, last_seen=? WHERE id=?", (status, now, t_id))
                await db.execute("INSERT INTO logs (target_id, event_type, timestamp) VALUES (?, ?, ?)", (t_id, status.upper(), now))
                
                if status == 'online' and target['notifications']:
                    try: await self.client.send_message('me', f"🚨 **SPECTRE ALERT**\nTarget: {target['display_name']}\nStatus: ONLINE")
                    except Exception as ne: logger.debug(f"Notification send failed: {ne}")

                if status == 'online':
                    await db.execute("INSERT INTO sessions (target_id, status, start_time) VALUES (?, 'ONLINE', ?)", (t_id, now))
                elif target['last_status'] == 'online':
                    async with db.execute("SELECT id, start_time FROM sessions WHERE target_id=? AND end_time IS NULL ORDER BY id DESC LIMIT 1", (t_id,)) as c:
                        open_sess = await c.fetchone()
                    if open_sess:
                        s = datetime.fromisoformat(open_sess['start_time']) if isinstance(open_sess['start_time'], str) else open_sess['start_time']
                        diff = now - s
                        dur = str(timedelta(seconds=int(diff.total_seconds())))
                        await db.execute("UPDATE sessions SET end_time=?, duration=?, status='FINISHED' WHERE id=?", (now, dur, open_sess['id']))
                await db.commit()
        except Exception as e: logger.debug(f"probe_target({t_id}): {e}")

cyber_bot = CyberTracker()

# --- ROUTES ---
def login_required(f):
    @wraps(f)
    async def decorated(*args, **kwargs):
        if 'user_id' not in session: return redirect('/login')
        return await f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    async def decorated(*args, **kwargs):
        if not session.get('is_admin'): abort(403)
        return await f(*args, **kwargs)
    return decorated

@app.route('/')
async def index(): return redirect('/dashboard')

@app.route('/login', methods=['GET', 'POST'])
async def login():
    msg = ""
    if request.method == 'POST':
        form = await request.form
        ip = request.headers.get('X-Forwarded-For', request.remote_addr or '0.0.0.0').split(',')[0].strip()

        # --- CSRF check ---
        if not validate_csrf(form.get('_csrf', '')):
            msg = "Invalid request. Please reload and try again."
        # --- Rate limit ---
        elif not check_rate_limit(ip):
            msg = "Too many attempts. Please wait 60 seconds."
        else:
            async with await get_db() as db:
                async with db.execute("SELECT * FROM users WHERE username = ?", (form.get('username'),)) as c:
                    user = await c.fetchone()
                if user and verify_password(form.get('password', ''), user['password']):
                    if user['expiry_date'] and datetime.now() > datetime.fromisoformat(user['expiry_date']):
                        msg = "Access expired."
                    else:
                        # Regenerate session to prevent fixation
                        old_csrf = session.get('_csrf')
                        session.clear()
                        session['_csrf']     = old_csrf or secrets.token_hex(24)
                        session['user_id']   = user['id']
                        session['is_admin']  = bool(user['is_admin'])
                        session['username']  = user['username']
                        return redirect('/dashboard')
                if not msg: msg = "Invalid credentials."

    csrf_token = generate_csrf()
    content = f"""
    <div class="login-bg">
        <div class="login-card">
            <div class="login-logo">
                <div class="login-logo-icon"><i class="fas fa-chess-knight"></i></div>
                <div class="login-title">SPECTRE</div>
                <div class="login-sub">Private Intelligence Ledger</div>
            </div>
            {f'<div class="alert alert-error"><i class="fas fa-circle-exclamation"></i> {e(msg)}</div>' if msg else ''}
            <form method="POST">
                {CSRF_INPUT.format(token=csrf_token)}
                <div class="form-group"><label>Agent Identity</label><input name="username" autocomplete="username" required placeholder="codename"></div>
                <div class="form-group"><label>Secure Passphrase</label><input type="password" name="password" autocomplete="current-password" required placeholder="••••••••"></div>
                <button class="btn btn-primary btn-full" style="padding:12px;">Authorize Entry</button>
            </form>
            <div style="margin-top:2rem; text-align:center;">
                <a href="/register" style="color:var(--text-muted); font-size:0.72rem; text-decoration:none; font-family:'Cinzel', serif; letter-spacing:0.1em; border-bottom:1px solid var(--gold-line); padding-bottom:1px;">Request New Identity</a>
            </div>
        </div>
    </div>
    """
    return await render_template_string(HTML_BASE.replace('%%CONTENT%%', content), hide_sidebar=True)

@app.route('/register', methods=['GET', 'POST'])
async def register():
    msg = ""
    if request.method == 'POST':
        f = await request.form
        try:
            async with await get_db() as db:
                await db.execute("INSERT INTO users (username, password, is_admin, max_targets, expiry_date) VALUES (?, ?, 0, 3, ?)", 
                                 (f.get('username'), hash_password(f.get('password', '')), datetime.now() + timedelta(days=14)))
                await db.commit()
            return redirect('/login')
        except: msg = "Identity conflict."
    
    content = f"""
    <div class="login-bg">
        <div class="login-card">
            <div class="login-logo">
                <div class="login-logo-icon"><i class="fas fa-user-plus"></i></div>
                <div class="login-title">INITIALIZE</div>
                <div class="login-sub">Create your secure dossier</div>
            </div>
            {f'<div class="alert alert-error"><i class="fas fa-circle-exclamation"></i> {msg}</div>' if msg else ''}
            <form method="POST">
                <div class="form-group"><label>Proposed Codename</label><input name="username" required placeholder="agent_handle"></div>
                <div class="form-group"><label>Passphrase</label><input type="password" name="password" required placeholder="••••••••"></div>
                <button class="btn btn-primary btn-full" style="padding:12px;">Establish Profile</button>
            </form>
            <div style="margin-top:2rem; text-align:center;">
                <a href="/login" style="color:var(--text-muted); font-size:0.72rem; text-decoration:none; font-family:'Cinzel', serif; letter-spacing:0.1em;">← Return to Login</a>
            </div>
        </div>
    </div>"""
    return await render_template_string(HTML_BASE.replace('%%CONTENT%%', content), hide_sidebar=True)

@app.route('/dashboard')
@login_required
async def dashboard():
    uid = session['user_id']; is_admin = session.get('is_admin')
    query = request.args.get('q', '').lower()
    
    async with await get_db() as db:
        if is_admin: q_sql = "SELECT t.*, u.username as owner FROM targets t LEFT JOIN users u ON t.owner_id = u.id ORDER BY t.last_status DESC"
        else: q_sql = "SELECT t.*, 'ME' as owner FROM targets t WHERE owner_id=? ORDER BY t.last_status DESC"
        async with db.execute(q_sql, (uid,) if not is_admin else ()) as c: targets = await c.fetchall()
        async with db.execute("SELECT * FROM users WHERE id=?", (uid,)) as c: me = await c.fetchone()

    if query: targets = [t for t in targets if query in t['display_name'].lower() or (t['tg_username'] and query in t['tg_username'].lower())]
    
    online_count = sum(1 for t in targets if t['last_status'] == 'online')
    total_count = len(targets)
    
    rows = ""
    for t in targets:
        st = t['last_status'] or 'unknown'
        badge_cls = {'online': 'badge-online', 'recently': 'badge-recently', 'offline': 'badge-offline'}.get(st, 'badge-unknown')
        ident = e(t['tg_id'] or t['phone'] or t['tg_username'] or '—')
        bell = '<i class="fas fa-bell text-gold" style="font-size:0.7rem;"></i>' if t['notifications'] else ''
        rows += f"""
        <tr class="clickable-row" onclick="window.location='/target/{t['id']}'">
            <td>
                <div class="identity-name">{e(t['display_name'])} {bell}</div>
                <div class="identity-id">{ident}</div>
            </td>
            <td><span class="badge {badge_cls}"><span class="badge-dot"></span>{st}</span></td>
            <td style="color:var(--text-muted); font-size:0.82rem;">{fmt_time(t['last_seen'])}</td>
            <td style="text-align:right; color:var(--border);"><i class="fas fa-chevron-right" style="font-size:0.75rem;"></i></td>
        </tr>"""

    empty = ""
    if not rows:
        empty = """<tr><td colspan="4">
            <div class="empty-state">
                <div class="empty-state-icon"><i class="fas fa-crosshairs"></i></div>
                <div class="empty-state-title">No targets in ledger</div>
                <div class="empty-state-sub">Add a target to begin surveillance.</div>
            </div>
        </td></tr>"""

    system_status = (
        '<span style="color:var(--success); font-size:0.75rem; font-family:Cinzel,serif; letter-spacing:0.08em;">● MONITORING ACTIVE</span>'
        if cyber_bot.tracking_active else
        f'<a href="/connect" class="btn btn-outline btn-sm" style="margin-top:0.5rem;">Reconnect →</a>'
    )

    content = f"""
    <div class="page-header">
        <div>
            <div class="page-eyebrow">Operations</div>
            <h1>Mission Control</h1>
            <div class="subtitle">Surveillance operations overview</div>
        </div>
        <div class="page-header-actions">
            <a href="/add" class="btn btn-primary"><i class="fas fa-plus"></i> New Target</a>
        </div>
    </div>

    <div class="stats-grid">
        <div class="stat-card">
            <i class="fas fa-crosshairs stat-card-icon"></i>
            <div class="stat-label">Total Targets</div>
            <div class="stat-val">{total_count}</div>
            <div class="stat-sub">of {me['max_targets']} quota</div>
        </div>
        <div class="stat-card">
            <i class="fas fa-signal stat-card-icon"></i>
            <div class="stat-label">Active Uplinks</div>
            <div class="stat-val" style="color:var(--success);">{online_count}</div>
            <div class="stat-sub">currently online</div>
        </div>
        <div class="stat-card">
            <i class="fas fa-satellite-dish stat-card-icon"></i>
            <div class="stat-label">System Status</div>
            <div class="stat-val" style="font-size:1.5rem; margin-bottom:0.5rem;">{'OPERATIONAL' if cyber_bot.tracking_active else 'OFFLINE'}</div>
            {system_status}
        </div>
    </div>

    <div class="panel">
        <div class="panel-header">
            <span class="section-title">Target Ledger</span>
            <div class="search-wrap">
                <i class="fas fa-search"></i>
                <form style="margin:0;">
                    <input name="q" placeholder="Search dossiers…" value="{query}" style="min-width:180px;">
                </form>
            </div>
        </div>
        <div class="table-wrap">
            <table>
                <thead><tr><th>Identity</th><th>Status</th><th>Last Seen</th><th></th></tr></thead>
                <tbody>{rows or empty}</tbody>
            </table>
        </div>
    </div>
    <script>setTimeout(function(){{ if(!document.querySelector('input[name=q]').value) location.reload(); }}, 30000);</script>
    """
    return await render_template_string(HTML_BASE.replace('%%CONTENT%%', content), active_page='dashboard')

# --- INTELLIGENCE ---
@app.route('/intelligence', methods=['GET', 'POST'])
@login_required
async def intelligence():
    uid = session['user_id']
    async with await get_db() as db:
        async with db.execute("SELECT id, display_name FROM targets WHERE owner_id=?", (uid,)) as c: my_targets = await c.fetchall()
    
    result_html = ""
    overlaps = None; prob = None
    if request.method == 'POST':
        _form = await request.form
        t1, t2 = _form.get('t1'), _form.get('t2')
        if t1 and t2:
            async with await get_db() as db:
                async with db.execute("SELECT start_time, end_time FROM sessions WHERE target_id=? ORDER BY id DESC LIMIT 50", (t1,)) as c: s1 = await c.fetchall()
                async with db.execute("SELECT start_time, end_time FROM sessions WHERE target_id=? ORDER BY id DESC LIMIT 50", (t2,)) as c: s2 = await c.fetchall()
            
            overlaps = 0
            for a in s1:
                for b in s2:
                    try:
                        a_s, a_e = datetime.fromisoformat(a[0]), datetime.fromisoformat(a[1]) if a[1] else now_tz()
                        b_s, b_e = datetime.fromisoformat(b[0]), datetime.fromisoformat(b[1]) if b[1] else now_tz()
                        if max(a_s, b_s) < min(a_e, b_e): overlaps += 1
                    except: pass
            
            prob = min(100, overlaps * 10)
            result_html = f"""<div class="stat-card" style="border-left:4px solid var(--gold); margin-top:2rem;">
                <h3 style="margin:0; font-family:'Cinzel', serif;">CORRELATION REPORT</h3>
                <div style="display:flex; gap:30px; margin-top:1.5rem;">
                    <div><div class="stat-label">INTERSECTIONS</div><div class="stat-val">{overlaps}</div></div>
                    <div><div class="stat-label">PROBABILITY</div><div class="stat-val">{prob}%</div></div>
                </div></div>"""

    opts = "".join([f"<option value='{t['id']}'>{t['display_name']}</option>" for t in my_targets])

    result_html = ""
    if overlaps is not None:
        bar_pct = min(100, prob)
        result_html = f"""
        <div class="panel" style="margin-top:1.5rem;">
            <div class="panel-header"><span class="section-title">Correlation Report</span></div>
            <div class="panel-body">
                <div style="display:grid; grid-template-columns:1fr 1fr 2fr; gap:1.5rem; align-items:center; flex-wrap:wrap;">
                    <div class="stat-card" style="margin:0;">
                        <div class="stat-label">Intersections</div>
                        <div class="stat-val">{overlaps}</div>
                        <div class="stat-sub">shared windows</div>
                    </div>
                    <div class="stat-card" style="margin:0;">
                        <div class="stat-label">Co-Presence</div>
                        <div class="stat-val">{prob}%</div>
                        <div class="stat-sub">estimated probability</div>
                    </div>
                    <div>
                        <div class="stat-label" style="margin-bottom:0.6rem;">Signal Strength</div>
                        <div style="background:var(--border-soft); height:8px; border-radius:4px; overflow:hidden;">
                            <div style="width:{bar_pct}%; height:100%; background:{'var(--success)' if bar_pct > 60 else 'var(--gold)' if bar_pct > 30 else 'var(--text-muted)'}; transition:width 0.6s ease;"></div>
                        </div>
                        <div style="font-size:0.72rem; color:var(--text-muted); font-style:italic; margin-top:0.4rem;">{'HIGH CORRELATION' if bar_pct > 60 else 'MODERATE SIGNAL' if bar_pct > 30 else 'WEAK SIGNAL'}</div>
                    </div>
                </div>
            </div>
        </div>"""

    no_targets = "" if my_targets else '<div class="alert alert-warn"><i class="fas fa-triangle-exclamation"></i> No targets found. Add targets from the dashboard first.</div>'

    content = f"""
    <div class="page-header">
        <div>
            <div class="page-eyebrow">Analysis</div>
            <h1>Co-Incidence Engine</h1>
            <div class="subtitle">Analyze behavioral intersections between subjects</div>
        </div>
    </div>
    {no_targets}
    <div class="panel">
        <div class="panel-header"><span class="section-title">Subject Correlation</span></div>
        <div class="panel-body">
            <form method="POST" style="display:grid; grid-template-columns:1fr 1fr auto; gap:1rem; align-items:flex-end; flex-wrap:wrap;">
                <div class="form-group" style="margin:0;"><label>Subject A</label><select name="t1">{opts}</select></div>
                <div class="form-group" style="margin:0;"><label>Subject B</label><select name="t2">{opts}</select></div>
                <button type="submit" class="btn btn-primary" style="height:44px; white-space:nowrap;"><i class="fas fa-magnifying-glass-chart"></i> Run Analysis</button>
            </form>
        </div>
    </div>
    {result_html}"""
    return await render_template_string(HTML_BASE.replace('%%CONTENT%%', content), active_page='intelligence')

@app.route('/settings', methods=['GET', 'POST'])
@login_required
async def settings():
    msg = ""
    if request.method == 'POST':
        f = await request.form
        global runtime_api_id, runtime_api_hash
        if f.get('api_id') and f.get('api_hash'):
            runtime_api_id = int(f.get('api_id')); runtime_api_hash = f.get('api_hash')
            async with await get_db() as db:
                await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('api_id', ?)", (str(runtime_api_id),))
                await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('api_hash', ?)", (runtime_api_hash,))
                await db.commit()
            msg = "CONFIGURATION UPDATED."
    
    content = f"""
    <div class="page-header">
        <div>
            <div class="page-eyebrow">System</div>
            <h1>Configuration</h1>
            <div class="subtitle">API credentials and connection state</div>
        </div>
    </div>
    <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap:1.5rem;">
        <div class="panel">
            <div class="panel-header"><span class="section-title">Telegram API</span></div>
            <div class="panel-body">
                <p style="color:var(--text-muted); font-size:0.82rem; margin-bottom:1.5rem; font-style:italic;">Credentials required for network access. Obtain from my.telegram.org.</p>
                {f'<div class="alert alert-ok"><i class="fas fa-check-circle"></i> {msg}</div>' if msg else ''}
                <form method="POST">
                    <div class="form-group"><label>API ID</label><input name="api_id" value="{runtime_api_id if runtime_api_id else ''}" placeholder="12345678"></div>
                    <div class="form-group"><label>API Hash</label><input name="api_hash" value="{runtime_api_hash if runtime_api_hash else ''}" placeholder="abc123..."></div>
                    <button type="submit" class="btn btn-primary">Save Configuration</button>
                </form>
            </div>
        </div>
        <div class="panel">
            <div class="panel-header"><span class="section-title">Connection State</span></div>
            <div class="panel-body">
                <div class="stat-label">Uplink Status</div>
                <div style="font-family:'Cormorant Garamond',serif; font-size:1.8rem; color:{'var(--success)' if cyber_bot.tracking_active else 'var(--danger)'}; margin:0.5rem 0 1rem;">{'Connected' if cyber_bot.tracking_active else 'Disconnected'}</div>
                <a href="/connect" class="btn btn-outline" style="width:auto;">{'Reset Uplink' if cyber_bot.tracking_active else 'Establish Connection'}</a>
            </div>
        </div>
    </div>"""
    return await render_template_string(HTML_BASE.replace('%%CONTENT%%', content), active_page='settings')

@app.route('/target/<int:t_id>', methods=['GET', 'POST'])
@login_required
async def target_detail(t_id):
    async with await get_db() as db:
        async with db.execute("SELECT * FROM targets WHERE id=?", (t_id,)) as c: t = await c.fetchone()
        if not t or (not session.get('is_admin') and t['owner_id'] != session['user_id']):
            return abort(403)
        async with db.execute("SELECT * FROM sessions WHERE target_id=? ORDER BY id DESC LIMIT 20", (t_id,)) as c: s = await c.fetchall()
        async with db.execute("SELECT COUNT(*) as cnt FROM sessions WHERE target_id=?", (t_id,)) as c:
            total_sessions = (await c.fetchone())['cnt']
        async with db.execute("SELECT SUM(CAST(SUBSTR(duration,1,INSTR(duration||':',':')-1)*3600 + SUBSTR(duration,INSTR(duration,':')+1,2)*60 AS INTEGER)) as secs FROM sessions WHERE target_id=? AND duration IS NOT NULL", (t_id,)) as c:
            row = await c.fetchone(); total_secs = row['secs'] or 0

    if request.args.get('toggle_alert'):
        async with await get_db() as db:
            await db.execute("UPDATE targets SET notifications=? WHERE id=?", (0 if t['notifications'] else 1, t_id))
            await db.commit()
        return redirect(url_for('target_detail', t_id=t_id))

    if request.method == 'POST':
        f = await request.form
        notes = f.get('notes', '')
        async with await get_db() as db:
            await db.execute("UPDATE targets SET notes=? WHERE id=?", (notes, t_id))
            await db.commit()
        return redirect(url_for('target_detail', t_id=t_id))

    # Compute total online time display
    h, rem = divmod(total_secs, 3600); m = rem // 60
    total_time_str = f"{h}h {m}m" if h > 0 else f"{m}m"

    # Heatmap data
    heatmap = await get_heatmap_data(t_id)
    heatmap_json = json.dumps(heatmap)

    sess_rows = "".join([
        f"<tr><td class='mono' style='font-size:0.85rem;'>{fmt_time(x['start_time'])}</td>"
        f"<td class='mono' style='font-size:0.85rem;'>{fmt_time(x['end_time'])}</td>"
        f"<td class='mono' style='color:var(--text-main); font-style:normal;'>{x['duration'] or 'ACTIVE'}</td></tr>"
        for x in s
    ])
    if not sess_rows:
        sess_rows = "<tr><td colspan='3' style='text-align:center; padding:2rem; color:var(--text-muted); font-style:italic;'>No sessions recorded yet.</td></tr>"

    alert_btn = f"""<a href="?toggle_alert=1" class="btn-outline" style="text-decoration:none; color:{'var(--success)' if t['notifications'] else 'var(--text-main)'}; border-color:{'var(--success)' if t['notifications'] else 'var(--text-muted)'};">{'● ALERTS ON' if t['notifications'] else 'ENABLE ALERTS'}</a>"""
    current_notes = t['notes'] or ''
    status_color = 'var(--success)' if t['last_status'] == 'online' else ('var(--gold)' if t['last_status'] == 'recently' else 'var(--text-muted)')

    content = f"""
    <div class="header-flex">
        <div>
            <a href="/dashboard" style="color:var(--text-muted); text-decoration:none; font-size:0.75rem; font-weight:600; letter-spacing:0.05em; font-family:'Cinzel', serif;">&larr; BACK TO DASHBOARD</a>
            <h1 style="margin-top:1rem;">{t['display_name']}</h1>
            <div style="color:var(--text-muted); margin-top:0.5rem; font-size:0.85rem; font-family:monospace;">ID: {t['tg_id'] or t['phone'] or t['tg_username'] or '—'}</div>
        </div>
        <div style="display:flex; gap:10px; margin-top:1rem; flex-wrap:wrap; align-items:center;">
            {alert_btn}
            <a href="/export/{t_id}" class="btn-outline" style="text-decoration:none;">EXPORT CSV</a>
            <a href="/delete/{t_id}" class="btn" style="background:#fff; border-color:#e5e7eb; color:var(--danger);" onclick="return confirm('Delete this target and all associated data?')">DELETE</a>
        </div>
    </div>

    <div class="stats-grid" style="grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); margin-bottom:2rem;">
        <div class="stat-card"><div class="stat-label">CURRENT STATUS</div><div class="stat-val" style="font-size:1.4rem; color:{status_color};">{(t['last_status'] or 'unknown').upper()}</div><div style="font-size:0.7rem; color:var(--text-muted); font-style:italic;">Last seen: {fmt_time(t['last_seen'])}</div></div>
        <div class="stat-card"><div class="stat-label">TOTAL SESSIONS</div><div class="stat-val">{total_sessions}</div><div style="font-size:0.7rem; color:var(--text-muted); font-style:italic;">recorded uplinks</div></div>
        <div class="stat-card"><div class="stat-label">TOTAL ONLINE TIME</div><div class="stat-val">{total_time_str}</div><div style="font-size:0.7rem; color:var(--text-muted); font-style:italic;">cumulative duration</div></div>
    </div>

    <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap:2rem; margin-bottom:2rem;">
        <div class="stat-card" style="padding-bottom:1.5rem;">
            <div style="font-family:'Cinzel', serif; font-weight:700; font-size:0.7rem; color:var(--accent); margin-bottom:1.2rem; letter-spacing:0.1em;">ACTIVITY HEATMAP (BY HOUR)</div>
            <canvas id="heatmapChart" height="100"></canvas>
        </div>
        <div class="stat-card">
            <div style="font-family:'Cinzel', serif; font-weight:700; font-size:0.7rem; color:var(--accent); margin-bottom:1.2rem; letter-spacing:0.1em;">INTELLIGENCE NOTES</div>
            <form method="POST">
                <textarea name="notes" style="height:130px; margin-bottom:1rem; resize:vertical;" placeholder="Add intelligence notes...">{current_notes}</textarea>
                <button type="submit" class="btn">SAVE NOTES</button>
            </form>
        </div>
    </div>

    <div class="table-wrap">
        <div style="padding:1.5rem; border-bottom:1px solid var(--border); font-family:'Cinzel', serif; font-weight:700; font-size:0.7rem; color:var(--accent);">SESSION LOG (LAST 20)</div>
        <table><tr><th>START</th><th>END</th><th>DURATION</th></tr>{sess_rows}</table>
    </div>

    <script>
    (function() {{
        const ctx = document.getElementById('heatmapChart').getContext('2d');
        const data = {heatmap_json};
        const labels = Array.from({{length:24}}, (_,i) => i===0?'12am':i<12?i+'am':i===12?'12pm':(i-12)+'pm');
        new Chart(ctx, {{
            type: 'bar',
            data: {{
                labels: labels,
                datasets: [{{ data: data, backgroundColor: data.map(v => v>0 ? `rgba(26,47,35,${{0.2 + v*0.08}})` : 'rgba(0,0,0,0.04)'), borderColor: 'transparent', borderRadius: 3 }}]
            }},
            options: {{
                plugins: {{ legend: {{ display: false }} }},
                scales: {{
                    y: {{ display: false, beginAtZero: true }},
                    x: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 9, family: 'Cinzel, serif' }}, color: '#999' }} }}
                }}
            }}
        }});
    }})();
    </script>
    """
    return await render_template_string(HTML_BASE.replace('%%CONTENT%%', content), active_page='dashboard')

@app.route('/add', methods=['GET', 'POST'])
@login_required
async def add():
    if request.method == 'POST':
        f = await request.form
        uid = session['user_id']
        tid = int(f.get('user_id')) if f.get('user_id') and f.get('user_id').isdigit() else 0
        async with await get_db() as db:
            async with db.execute("SELECT max_targets FROM users WHERE id=?", (uid,)) as c:
                me = await c.fetchone()
            async with db.execute("SELECT COUNT(*) as cnt FROM targets WHERE owner_id=?", (uid,)) as c:
                cnt = (await c.fetchone())['cnt']
            if me and cnt >= me['max_targets']:
                return redirect('/dashboard?err=quota')
            await db.execute("INSERT INTO targets (owner_id, tg_id, tg_username, phone, display_name, last_status) VALUES (?,?,?,?,?, 'unknown')", 
                             (uid, tid, f.get('username'), f.get('phone'), f.get('name')))
            await db.commit()
        return redirect('/dashboard')

    content = """
    <div style="max-width:600px; margin:auto;">
        <h1>Initialize Target.</h1>
        <div class="subtitle">PROVIDE IDENTIFIERS.</div>
        <div class="stat-card">
            <form method="POST">
                <div class="form-group"><label>TARGET DESIGNATION (NAME)</label><input name="name" required placeholder="SUBJECT ALPHA"></div>
                <div style="border-top:1px solid var(--border); margin: 2rem 0; position:relative;"><span style="position:absolute; top:-10px; left:0; background:var(--surface); padding-right:10px; font-size:0.6rem; color:var(--text-muted); font-family:'Cinzel', serif;">IDENTIFIERS</span></div>
                <div style="display:grid; grid-template-columns: 1fr 1fr; gap:1rem;">
                    <div class="form-group"><label style="color:var(--success);">PRIORITY 1: NUMERIC ID</label><input name="user_id" placeholder="123456789"></div>
                    <div class="form-group"><label style="color:var(--success);">PRIORITY 1: PHONE</label><input name="phone" placeholder="+1..."></div>
                </div>
                <div class="form-group"><label>PRIORITY 2: USERNAME</label><input name="username" placeholder="@target_handle"></div>
                <button class="btn">BEGIN SURVEILLANCE</button>
            </form>
        </div>
    </div>"""
    return await render_template_string(HTML_BASE.replace('%%CONTENT%%', content), active_page='dashboard')

# --- LOGS & USERS ---
@app.route('/logs')
@login_required
async def logs():
    async with await get_db() as db:
        q = "SELECT l.*, t.display_name FROM logs l LEFT JOIN targets t ON l.target_id = t.id ORDER BY l.timestamp DESC LIMIT 100"
        async with db.execute(q) as c: logs = await c.fetchall()
    rows = "".join([f"<tr><td class='mono' style='font-size:0.85rem;'>{fmt_time(l['timestamp'])}</td><td>{l['display_name'] or 'SYSTEM'}</td><td style='font-weight:600; color:{'var(--success)' if l['event_type'] == 'ONLINE' else 'var(--text-muted)'}'>{l['event_type']}</td></tr>" for l in logs])
    content = f"<h1>Audit Logs</h1><div class='subtitle'>SYSTEM ARCHIVE.</div><div class='table-wrap'><table><tr><th>TIME</th><th>ENTITY</th><th>EVENT</th></tr>{rows}</table></div>"
    return await render_template_string(HTML_BASE.replace('%%CONTENT%%', content), active_page='logs')

@app.route('/users', methods=['GET', 'POST'])
@login_required
@admin_required
async def users():
    if request.method == 'POST':
        f = await request.form
        if 'delete' in f:
            async with await get_db() as db: await db.execute("DELETE FROM users WHERE id=?", (f['delete'],)); await db.commit()
        else:
            try:
                async with await get_db() as db:
                    await db.execute("INSERT INTO users (username, password, is_admin, max_targets) VALUES (?, ?, ?, ?)", 
                                     (f['username'], hash_password(f['password']), 1 if f.get('role')=='admin' else 0, int(f['max_targets'])))
                    await db.commit()
            except: pass
    async with await get_db() as db:
        async with db.execute("SELECT * FROM users") as c: users = await c.fetchall()
    rows = "".join([f"<tr><td>{u['username']}</td><td>{'ADMIN' if u['is_admin'] else 'USER'}</td><td>{u['max_targets']}</td><td>{f'<form method=POST style=display:inline><input type=hidden name=delete value={u['id']}><button class=btn-outline style=padding:4px;color:var(--danger)>REMOVE</button></form>' if u['id'] != session['user_id'] else ''}</td></tr>" for u in users])
    content = f"<h1>User Management</h1><div class='subtitle'>ACCESS CONTROL.</div><div style='display:grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap:2rem;'><div class='table-wrap'><table><tr><th>IDENTITY</th><th>ROLE</th><th>QUOTA</th><th>ACTION</th></tr>{rows}</table></div><div class='stat-card'><h2 style='margin-top:0;'>Provision</h2><form method='POST'><div class='form-group'><label>USER</label><input name='username' required></div><div class='form-group'><label>PASS</label><input name='password' required></div><div class='form-group'><label>ROLE</label><select name='role'><option value='user'>USER</option><option value='admin'>ADMIN</option></select></div><div class='form-group'><label>QUOTA</label><input name='max_targets' type='number' value='5'></div><button class='btn'>CREATE</button></form></div></div>"
    return await render_template_string(HTML_BASE.replace('%%CONTENT%%', content), active_page='users')

@app.route('/connect', methods=['GET', 'POST'])
@login_required
async def connect():
    if request.method == 'POST':
        f = await request.form
        global runtime_api_id, runtime_api_hash, temp_client, phone_number, phone_code_hash
        runtime_api_id = int(f.get('api_id')); runtime_api_hash = f.get('api_hash')
        await save_setting('api_id', runtime_api_id); await save_setting('api_hash', runtime_api_hash)
        try:
            temp_client = TelegramClient(StringSession(), runtime_api_id, runtime_api_hash)
            await temp_client.connect(); phone_number = f.get('phone'); send = await temp_client.send_code_request(phone_number); phone_code_hash = send.phone_code_hash; return redirect('/verify')
        except Exception as e: return f"Error: {e}"
    content = """<div class="stat-card" style="max-width:500px; margin:auto; margin-top:2rem;"><h2>Establish Connection</h2><form method="POST"><div class="form-group"><label>API ID</label><input name="api_id" required></div><div class="form-group"><label>API Hash</label><input name="api_hash" required></div><div class="form-group"><label>Phone</label><input name="phone" required></div><button class="btn">Request OTP</button></form></div>"""
    return await render_template_string(HTML_BASE.replace('%%CONTENT%%', content), active_page='settings')

@app.route('/verify', methods=['GET', 'POST'])
@login_required
async def verify():
    if request.method == 'POST':
        try:
            await temp_client.sign_in(phone=phone_number, code=(await request.form).get('code'), phone_code_hash=phone_code_hash)
            await save_setting('session_string', temp_client.session.save())
            await temp_client.disconnect(); asyncio.create_task(cyber_bot.start()); return redirect('/dashboard')
        except: return "Invalid OTP"
    content = """<div class="stat-card" style="max-width:400px; margin:auto; margin-top:2rem;"><h2>Verify Identity</h2><form method="POST"><div class="form-group"><label>OTP Code</label><input name="code" required></div><button class="btn">Confirm</button></form></div>"""
    return await render_template_string(HTML_BASE.replace('%%CONTENT%%', content), active_page='settings')

@app.route('/delete/<int:t_id>')
@login_required
async def delete(t_id):
    async with await get_db() as db:
        async with db.execute("SELECT owner_id FROM targets WHERE id=?", (t_id,)) as c:
            row = await c.fetchone()
        if not row or (not session.get('is_admin') and row['owner_id'] != session['user_id']):
            return abort(403)
        await db.execute("DELETE FROM sessions WHERE target_id=?", (t_id,))
        await db.execute("DELETE FROM logs WHERE target_id=?", (t_id,))
        await db.execute("DELETE FROM targets WHERE id=?", (t_id,))
        await db.commit()
    return redirect('/dashboard')

@app.route('/export/<int:t_id>')
@login_required
async def export(t_id):
    async with await get_db() as db:
        async with db.execute("SELECT owner_id FROM targets WHERE id=?", (t_id,)) as c:
            row = await c.fetchone()
        if not row or (not session.get('is_admin') and row['owner_id'] != session['user_id']):
            return abort(403)
        async with db.execute("SELECT * FROM sessions WHERE target_id=? ORDER BY id DESC", (t_id,)) as c: rows = await c.fetchall()
    si = io.StringIO(); cw = csv.writer(si); cw.writerow(['Status', 'Start', 'End', 'Duration'])
    for r in rows: cw.writerow([r['status'], r['start_time'], r['end_time'], r['duration']])
    return Response(si.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=log_{t_id}.csv"})

@app.route('/logout')
async def logout(): session.clear(); return redirect('/login')

@app.before_serving
async def startup():
    await init_db()
    asyncio.create_task(cyber_bot.start())

@app.after_serving
async def shutdown():
    await cyber_bot.stop()

if __name__ == "__main__":
    config = Config(); config.bind = [f"0.0.0.0:{os.getenv('PORT', '8000')}"]; asyncio.run(serve(app, config))
