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
| `pdf_generator.py` | Monthly overview, daily, and meeting note pages with hyperlinks; `build_answer_pdf` renders a Claude reply |
| `rm_uploader.py` | Uploads/downloads PDFs & notebooks to/from reMarkable cloud (rmapi CLI) |
| `rm_notebook.py` | Unpacks a downloaded notebook and renders its latest `.rm` page to a PNG |
| `claude_assistant.py` | "Ask Claude about a handwritten page": notebook → vision API → answer PDF → device |
| `main.py` | CLI entry point |
| `templates/` | base, dashboard, calendars, history, settings |
| `deploy.sh` | Single-command Proxmox LXC container setup |
| `polarisfolio.service` | systemd unit file |
| `nginx.conf` | Reverse proxy config |
| `update.sh` | Auto-deploy: git pull + restart if changed (run by webhook/timer) |
| `polarisfolio-update.{service,timer}` | Daily safety-net auto-update timer |

## Running locally
```bash
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```
Open http://localhost:8000

## Key design decisions
- Single-user app — no auth wall, no multi-tenancy
- Planner generation runs via the scheduler (`scheduler._scheduled_generate`), triggered by the daily schedule or the Settings "Run now" button (`POST /api/schedule/run-now`). There is no ad-hoc generate page.
- MSAL token cache stored in `~/.polarisfolio_msal_cache.json`
- PDFs stored in `~/polarisfolio_pdfs/`
- DB at `~/.polarisfolio_web.db` (override with `POLARISFOLIO_DB` env var)
- Timezone: events are stored/compared as UTC; user-facing display uses the timezone setting (see `settings` table key `timezone`)
- Scheduled sync has two modes (`settings` key `sync_mode`):
  - `rolling` (default): one fixed-geometry yearly planner (`pdf_generator.build_yearly_planner`) re-synced **in place** via `rmapi put --content-only`, which keeps the document ID and every `.rm` handwriting layer. Requires constant page count/order/size for the year, so each meeting gets a permanent page slot persisted in the `meeting_slots` table (`database.assign_meeting_slots`) — a meeting's note page (and its ink) stays put even when it is rescheduled. The geometry signature (`yearly_geometry_signature`) gates in-place update vs. full recreate (`--force`).
  - `dated`: a new dated document per run, old ones pruned (`RemarkableUploader.prune_old_dated`, key `schedule_keep_days`).

## Not yet built
- Auth/login wall for web UI
- reMarkable official Connect API (currently uses unofficial rmapi / REST fallback)
- Automated cron scheduling (see `scheduler.py` — APScheduler-based, add via settings)
