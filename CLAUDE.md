# PolarisFolio — Claude Code context

Self-hosted reMarkable Paper Pro planner app. Pulls calendar events from Microsoft 365 (Graph API OAuth) and/or ICS feeds, generates a hyperlinked PDF planner, and uploads it to a reMarkable Paper Pro via the unofficial reMarkable cloud API.

## Stack
- FastAPI backend with Jinja2 server-rendered templates
- SQLite via aiosqlite
- reportlab for PDF generation
- msal for Microsoft OAuth
- Deployment: Proxmox LXC container, nginx reverse proxy, systemd service

## File map
| File | Purpose |
|------|---------|
| `app.py` | FastAPI routes and OAuth flow |
| `database.py` | SQLite layer (settings, ICS feeds, upload history) |
| `models.py` | CalendarEvent and Attendee dataclasses |
| `graph_connector.py` | Microsoft Graph API calendar pull |
| `ical_connector.py` | ICS feed fetch and parse |
| `calendar_manager.py` | Combines both sources, deduplicates, groups by day |
| `pdf_generator.py` | Monthly overview, daily, and meeting note pages with hyperlinks |
| `rm_uploader.py` | Uploads PDF to reMarkable cloud (rmapi CLI primary, REST API fallback) |
| `main.py` | CLI entry point |
| `templates/` | base, dashboard, calendars, generate, history, settings |
| `deploy.sh` | Single-command Proxmox LXC container setup |
| `polarisfolio.service` | systemd unit file |
| `nginx.conf` | Reverse proxy config |

## Running locally
```bash
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```
Open http://localhost:8000

## Key design decisions
- Single-user app — no auth wall, no multi-tenancy
- Background task pattern for PDF generation (FastAPI BackgroundTasks) — generate redirects immediately to /history
- MSAL token cache stored in `~/.polarisfolio_msal_cache.json`
- PDFs stored in `~/polarisfolio_pdfs/`
- DB at `~/.polarisfolio_web.db` (override with `POLARISFOLIO_DB` env var)
- Timezone: events are stored/compared as UTC; user-facing display uses the timezone setting (see `settings` table key `timezone`)

## Not yet built
- Auth/login wall for web UI
- reMarkable official Connect API (currently uses unofficial rmapi / REST fallback)
- Automated cron scheduling (see `scheduler.py` — APScheduler-based, add via settings)
