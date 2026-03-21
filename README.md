# SPECTRE — Private Intelligence Ledger

A self-hosted Telegram online-status tracker with a polished web dashboard.

---

## Features

- **Real-time tracking** — polls Telegram every 5 s, records online/offline/recently sessions
- **Activity heatmap** — per-target hourly activity chart
- **Co-incidence engine** — overlap analysis between any two targets
- **Multi-user** — admin can provision agents with per-user target quotas and expiry dates
- **Notifications** — Telegram self-message alert when a target comes online
- **CSV export** — full session history per target
- **Old-money UI** — responsive sidebar + mobile bottom-nav, dark green & gold palette

---

## Security Highlights

| Feature | Detail |
|---|---|
| Password hashing | PBKDF2-SHA256, 100 000 iterations, random salt |
| CSRF protection | Per-session token on all POST forms |
| Login rate-limiting | 10 attempts / 60 s per IP (in-memory) |
| Session fixation | Session regenerated on login |
| XSS escaping | All user values escaped before HTML interpolation |
| Ownership checks | Every target/export/delete verifies the requesting user |
| Cascade delete | Removing a target also removes its sessions and logs |
| Quota enforcement | Server-side max_targets check on add |
| Orphan sessions | Open sessions closed on startup (crash recovery) |
| Graceful shutdown | Telegram client disconnected cleanly on exit |
| Exponential backoff | Tracker backs off up to 5 min on repeated errors |
| DB indexes | Indexes on target_id, timestamp, owner_id for fast queries |

---

## Requirements

- Python 3.10+
- A Telegram account + API credentials from [my.telegram.org](https://my.telegram.org)

---

## Setup

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/protracker.git
cd protracker

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env — set API_ID, API_HASH, ADMIN_PASS, SECRET_KEY

# 4. Generate a Telegram session string
python3 generate_session.py
# Paste the output into your .env as SESSION_STRING (or enter it via /connect in the UI)

# 5. Run
python3 main.py
# Open http://localhost:8000
```

---

## Deployment (Render / Railway / Fly.io)

Set the following environment variables in your hosting dashboard:

```
API_ID
API_HASH
ADMIN_USER
ADMIN_PASS
SECRET_KEY
PORT
```

The app binds to `0.0.0.0:$PORT` automatically.

---

## Project Structure

```
main.py              — Application entry point (all routes, tracker engine, UI)
generate_session.py  — One-time Telegram session string generator
requirements.txt     — Python dependencies
.env.example         — Environment variable template
```

---

## Changelog (vs original)

- Passwords hashed (PBKDF2-SHA256) — were stored plain-text
- CSRF tokens on all forms
- Login brute-force rate limiting
- Session regeneration on login
- XSS escaping on all user-supplied HTML interpolations
- `intelligence` route fixed — `request.form` was not awaited (form never worked)
- `add` route enforces `max_targets` quota server-side
- `delete` route cascades to sessions + logs; ownership verified
- `export` route ownership check added
- `connect` / `settings` global mutation protected with `asyncio.Lock`
- Notes textarea wired to `POST /target/<id>` (was a dead button)
- Activity heatmap rendered with Chart.js on target detail page
- Summary stats (total sessions, total online time) on target detail page
- Dashboard auto-refreshes every 30 s (skips if search is active)
- Quota warning banner + toast notifications
- Orphan open-sessions closed on startup (crash recovery)
- DB indexes on all hot query columns
- Exponential backoff in tracker loop (caps at 5 min)
- Graceful Telegram client disconnect on server shutdown
- Full UI/UX redesign — new design system, mobile bottom-nav, animated badges, empty states
- `%%CONTENT%%` placeholder replaces `{{ CONTENT }}` to avoid substitution collision
- Unused imports (`sys`, `time`, `random`) removed
- All bare `except: pass` replaced with logged exceptions
