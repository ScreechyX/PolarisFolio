"""
PolarisFolio Web - FastAPI application.

Routes:
  GET  /                  - dashboard
  GET  /auth/microsoft    - start Microsoft OAuth
  GET  /auth/callback     - OAuth callback
  GET  /auth/disconnect   - remove Microsoft token
  GET  /calendars         - manage calendar sources
  POST /calendars/ical    - add ICS feed
  POST /calendars/ical/{id}/toggle - toggle feed on/off
  POST /calendars/ical/{id}/delete - remove feed
  GET  /history           - past planners
  GET  /download/{id}     - download a generated PDF
  GET  /settings          - app settings
  POST /settings          - save settings
"""

import os
import shutil
import uuid
import asyncio
import hmac
import hashlib
import json
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import msal

from database import (
    init_db, get_setting, set_setting, get_all_settings,
    get_ical_feeds, add_ical_feed, toggle_ical_feed, delete_ical_feed,
    get_uploads, clear_uploads,
)
from scheduler import scheduler, apply_schedule

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="PolarisFolio")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

PDF_DIR = os.path.expanduser("~/polarisfolio_pdfs")
os.makedirs(PDF_DIR, exist_ok=True)

# Simple daily cache for dashboard event data
_dashboard_cache: dict = {"date": None, "upcoming_by_day": []}

MS_SCOPES = ["Calendars.Read", "User.Read"]
MS_AUTHORITY = "https://login.microsoftonline.com/common"
TOKEN_CACHE_FILE = os.path.expanduser("~/.polarisfolio_msal_cache.json")


@app.on_event("startup")
async def startup():
    await init_db()
    scheduler.start()
    await apply_schedule()


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _load_msal_cache():
    cache = msal.SerializableTokenCache()
    if os.path.exists(TOKEN_CACHE_FILE):
        with open(TOKEN_CACHE_FILE) as f:
            cache.deserialize(f.read())
    return cache


def _save_msal_cache(cache: msal.SerializableTokenCache):
    if cache.has_state_changed:
        with open(TOKEN_CACHE_FILE, "w") as f:
            f.write(cache.serialize())


def _get_msal_app(client_id: str, cache=None):
    return msal.ConfidentialClientApplication(
        client_id,
        authority=MS_AUTHORITY,
        client_credential=None,
        token_cache=cache,
    )


async def get_ms_token() -> str | None:
    """Returns a valid MS access token, or None if not authenticated."""
    client_id = await get_setting("ms_client_id")
    if not client_id:
        return None

    cache = _load_msal_cache()
    app = msal.PublicClientApplication(
        client_id, authority=MS_AUTHORITY, token_cache=cache
    )
    accounts = app.get_accounts()
    if not accounts:
        return None

    result = app.acquire_token_silent(MS_SCOPES, account=accounts[0])
    _save_msal_cache(cache)

    if result and "access_token" in result:
        return result["access_token"]
    return None


async def ms_connected() -> bool:
    return await get_ms_token() is not None


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    ms_ok = await ms_connected()
    feeds = await get_ical_feeds()
    uploads = await get_uploads(limit=5)

    # Upcoming events — next 3 days from all sources, cached until midnight
    from calendar_manager import CalendarManager
    from zoneinfo import ZoneInfo
    from collections import defaultdict
    tz_name = await get_setting("timezone") or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    today = datetime.now(tz).date()
    upcoming_by_day = []

    if _dashboard_cache["date"] == today:
        upcoming_by_day = _dashboard_cache["upcoming_by_day"]
    else:
        try:
            manager = CalendarManager()
            if ms_ok:
                token = await get_ms_token()
                manager.add_graph_source(token)
            for feed in feeds:
                if feed["enabled"] and feed.get("url"):
                    manager.add_ical_source(feed["url"], name=feed.get("name", "ICS Feed"))

            now = datetime.now(timezone.utc)
            events = manager.get_events(now, now + timedelta(days=3))

            by_day: dict = defaultdict(list)
            for e in events:
                if not e.is_all_day:
                    day_key = e.start.astimezone(tz).strftime("%Y-%m-%d")
                    by_day[day_key].append(e)

            for i in range(3):
                d = today + timedelta(days=i)
                key = d.strftime("%Y-%m-%d")
                label = "Today" if i == 0 else "Tomorrow" if i == 1 else d.strftime("%A")
                date_str = d.strftime("%-d %B")
                upcoming_by_day.append((label, date_str, by_day.get(key, [])))

            _dashboard_cache["date"] = today
            _dashboard_cache["upcoming_by_day"] = upcoming_by_day
        except Exception:
            pass

    upcoming = [e for _, _, evts in upcoming_by_day for e in evts]

    return templates.TemplateResponse(request, "dashboard.html", {
        "ms_connected": ms_ok,
        "feed_count": len([f for f in feeds if f["enabled"]]),
        "uploads": uploads,
        "upcoming": upcoming,
        "upcoming_by_day": upcoming_by_day,
        "active": "dashboard",
    })


# ---------------------------------------------------------------------------
# Microsoft OAuth
# ---------------------------------------------------------------------------

@app.get("/auth/microsoft")
async def auth_microsoft(request: Request):
    client_id = await get_setting("ms_client_id")
    if not client_id:
        return RedirectResponse("/settings?error=no_client_id")

    redirect_uri = str(request.base_url) + "auth/callback"
    if request.headers.get("x-forwarded-proto") == "https":
        redirect_uri = redirect_uri.replace("http://", "https://", 1)
    cache = _load_msal_cache()
    ms_app = msal.PublicClientApplication(
        client_id, authority=MS_AUTHORITY, token_cache=cache
    )

    flow = ms_app.initiate_auth_code_flow(
        scopes=MS_SCOPES,
        redirect_uri=redirect_uri,
    )
    _save_msal_cache(cache)

    # Store flow state in a simple file (single-user, so this is fine)
    import json
    with open(os.path.expanduser("~/.polarisfolio_auth_flow.json"), "w") as f:
        json.dump(flow, f)

    return RedirectResponse(flow["auth_uri"])


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = None, error: str = None):
    if error:
        return RedirectResponse(f"/settings?error={error}")

    import json
    flow_file = os.path.expanduser("~/.polarisfolio_auth_flow.json")
    if not os.path.exists(flow_file):
        return RedirectResponse("/settings?error=flow_missing")

    with open(flow_file) as f:
        flow = json.load(f)

    client_id = await get_setting("ms_client_id")
    cache = _load_msal_cache()
    ms_app = msal.PublicClientApplication(
        client_id, authority=MS_AUTHORITY, token_cache=cache
    )

    redirect_uri = str(request.base_url) + "auth/callback"
    if request.headers.get("x-forwarded-proto") == "https":
        redirect_uri = redirect_uri.replace("http://", "https://", 1)
    result = ms_app.acquire_token_by_auth_code_flow(
        flow,
        dict(request.query_params),
        redirect_uri=redirect_uri,
    )
    _save_msal_cache(cache)
    os.unlink(flow_file)

    if "access_token" in result:
        # Save display name + account email (for the "you" chip on meeting pages)
        claims = result.get("id_token_claims", {})
        name = claims.get("name", "")
        if name:
            await set_setting("ms_user_name", name)
        email = claims.get("preferred_username", "") or claims.get("email", "")
        if email:
            await set_setting("ms_user_email", email)
        return RedirectResponse("/settings?success=microsoft_connected")

    return RedirectResponse(f"/settings?error=auth_failed")


@app.get("/auth/disconnect")
async def auth_disconnect():
    if os.path.exists(TOKEN_CACHE_FILE):
        os.unlink(TOKEN_CACHE_FILE)
    await set_setting("ms_user_name", "")
    return RedirectResponse("/settings?success=disconnected")


# ---------------------------------------------------------------------------
# Calendars
# ---------------------------------------------------------------------------

@app.get("/calendars", response_class=HTMLResponse)
async def calendars_page(request: Request, success: str = None, error: str = None):
    ms_ok = await ms_connected()
    ms_user = await get_setting("ms_user_name", "")
    feeds = await get_ical_feeds()

    return templates.TemplateResponse(request, "calendars.html", {
        "ms_connected": ms_ok,
        "ms_user": ms_user,
        "feeds": feeds,
        "success": success,
        "error": error,
        "active": "calendars",
    })


@app.post("/calendars/ical")
async def add_ical(
    name: str = Form(...),
    url: str = Form(...),
):
    if not url.startswith("http"):
        return RedirectResponse("/calendars?error=invalid_url", status_code=303)
    await add_ical_feed(name.strip(), url.strip())
    return RedirectResponse("/calendars?success=feed_added", status_code=303)


@app.post("/calendars/ical/{feed_id}/toggle")
async def toggle_feed(feed_id: int, enabled: int = Form(0)):
    await toggle_ical_feed(feed_id, bool(enabled))
    return RedirectResponse("/calendars", status_code=303)


@app.post("/calendars/ical/{feed_id}/delete")
async def delete_feed(feed_id: int):
    await delete_ical_feed(feed_id)
    return RedirectResponse("/calendars?success=feed_removed", status_code=303)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

@app.post("/webhook/github")
async def github_webhook(request: Request):
    """
    Auto-deploy hook: GitHub calls this on every push. We verify the HMAC
    signature, and on a push to the configured branch fire update.sh
    (git pull + restart) detached so it survives our own restart.

    Configure the shared secret via the GITHUB_WEBHOOK_SECRET env var (set it
    in the systemd unit) or the 'github_webhook_secret' setting. The deploy
    branch is GITHUB_DEPLOY_BRANCH / 'github_webhook_branch' (default 'main').
    """
    secret = (os.environ.get("GITHUB_WEBHOOK_SECRET", "")
              or await get_setting("github_webhook_secret", ""))
    if not secret:
        return JSONResponse({"error": "webhook not configured"}, status_code=503)

    body = await request.body()
    sent_sig = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sent_sig, expected):
        return JSONResponse({"error": "bad signature"}, status_code=401)

    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        return JSONResponse({"ok": True, "pong": True})
    if event != "push":
        return JSONResponse({"ok": True, "ignored_event": event})

    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return JSONResponse({"error": "bad payload"}, status_code=400)

    branch = (os.environ.get("GITHUB_DEPLOY_BRANCH", "")
              or await get_setting("github_webhook_branch", "") or "main")
    if payload.get("ref") != f"refs/heads/{branch}":
        return JSONResponse({"ok": True, "ignored_ref": payload.get("ref")})

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "update.sh")
    if not os.path.exists(script):
        return JSONResponse({"error": "update.sh missing"}, status_code=500)

    # Detached so it outlives the service restart it triggers
    subprocess.Popen(
        ["/bin/bash", script],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return JSONResponse({"ok": True, "deploying": True, "branch": branch})


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request, success: str = None):
    uploads = await get_uploads(limit=50)
    return templates.TemplateResponse(request, "history.html", {
        "uploads": uploads,
        "success": success,
        "active": "history",
    })


@app.post("/history/clear")
async def clear_history(delete_files: str = Form("off")):
    await clear_uploads(delete_files=delete_files == "on")
    return RedirectResponse("/history?success=cleared", status_code=303)


@app.get("/download/{upload_id}")
async def download_pdf(upload_id: int):
    uploads = await get_uploads(limit=200)
    match = next((u for u in uploads if u["id"] == upload_id), None)
    if not match or not match["pdf_path"] or not os.path.exists(match["pdf_path"]):
        return JSONResponse({"error": "File not found"}, status_code=404)
    return FileResponse(
        match["pdf_path"],
        media_type="application/pdf",
        filename=os.path.basename(match["pdf_path"]),
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, success: str = None, error: str = None):
    settings = await get_all_settings()
    ms_ok = await ms_connected()

    # Rolling-sync status: live document, slot usage, and whether the next run
    # will update in place or recreate (which resets handwriting).
    rolling = None
    if settings.get("sync_mode", "rolling") == "rolling":
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from database import count_meeting_slots
        from pdf_generator import yearly_geometry_signature
        try:
            tz = ZoneInfo(settings.get("timezone") or "UTC")
        except Exception:
            tz = ZoneInfo("UTC")
        year = datetime.now(tz).year
        try:
            slot_count = int(settings.get("sync_meeting_slots", "200"))
        except (TypeError, ValueError):
            slot_count = 200
        used = await count_meeting_slots(year)
        sig = yearly_geometry_signature(year, slot_count)
        live_sig = settings.get("sync_live_sig", "")
        rolling = {
            "doc_name": (settings.get("rm_doc_name") or f"PolarisFolio {year}").strip(),
            "year": year,
            "slot_count": slot_count,
            "slots_used": used,
            "slots_pct": round(100 * used / slot_count) if slot_count else 0,
            "next_in_place": bool(live_sig) and live_sig == sig,
            "live": bool(live_sig),
        }

    return templates.TemplateResponse(request, "settings.html", {
        "settings": settings,
        "ms_connected": ms_ok,
        "rmapi_ok": shutil.which("rmapi") is not None,
        "rolling": rolling,
        "success": success,
        "error": error,
        "active": "settings",
    })


@app.post("/settings")
async def save_settings(
    ms_client_id: str = Form(""),
    rm_email: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    smtp_user: str = Form(""),
    smtp_pass: str = Form(""),
    rm_folder: str = Form("/PolarisFolio"),
    timezone: str = Form("UTC"),
    schedule_enabled: str = Form("0"),
    schedule_day: str = Form("mon"),
    schedule_hour: str = Form("7"),
    schedule_weeks_ahead: str = Form("2"),
    schedule_upload: str = Form("0"),
    sync_mode: str = Form("rolling"),
    sync_meeting_slots: str = Form("200"),
    slot_filter: str = Form("attendees"),
    schedule_keep_days: str = Form("5"),
):
    if ms_client_id:
        await set_setting("ms_client_id", ms_client_id.strip())
    await set_setting("rm_email", rm_email.strip())
    await set_setting("smtp_host", smtp_host.strip())
    await set_setting("smtp_port", smtp_port.strip() or "587")
    await set_setting("smtp_user", smtp_user.strip())
    if smtp_pass:
        await set_setting("smtp_pass", smtp_pass.strip())
    await set_setting("rm_folder", rm_folder.strip() or "/PolarisFolio")
    await set_setting("timezone", timezone.strip() or "UTC")
    await set_setting("schedule_enabled", schedule_enabled)
    await set_setting("schedule_day", schedule_day)
    await set_setting("schedule_hour", schedule_hour)
    await set_setting("schedule_weeks_ahead", schedule_weeks_ahead)
    await set_setting("schedule_upload", schedule_upload)
    await set_setting("sync_mode", sync_mode if sync_mode in ("rolling", "dated") else "rolling")
    try:
        slots = max(0, min(1000, int(sync_meeting_slots)))
    except (TypeError, ValueError):
        slots = 200
    await set_setting("sync_meeting_slots", str(slots))
    await set_setting("slot_filter", slot_filter if slot_filter in ("attendees", "all") else "attendees")
    try:
        keep = max(0, int(schedule_keep_days))
    except (TypeError, ValueError):
        keep = 5
    await set_setting("schedule_keep_days", str(keep))

    await apply_schedule()

    return RedirectResponse("/settings?success=saved", status_code=303)


# ---------------------------------------------------------------------------
# API - trigger scheduled generation immediately
# ---------------------------------------------------------------------------

@app.post("/api/schedule/run-now")
async def schedule_run_now(background_tasks: BackgroundTasks):
    from scheduler import _scheduled_generate
    background_tasks.add_task(_scheduled_generate)
    return {"status": "started"}


# ---------------------------------------------------------------------------
# API - debug
# ---------------------------------------------------------------------------

@app.get("/api/debug/events")
async def debug_events(start: str, end: str):
    """Debug: show all events parsed from sources for a date range."""
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except ValueError:
        return JSONResponse({"error": "invalid dates"})

    from zoneinfo import ZoneInfo
    from calendar_manager import CalendarManager
    tz = ZoneInfo(await get_setting("timezone", "UTC"))
    start_dt = datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0, tzinfo=tz)
    end_dt = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=tz)

    manager = CalendarManager()
    token = await get_ms_token()
    if token:
        manager.add_graph_source(token)
    feeds = await get_ical_feeds(enabled_only=True)
    for f in feeds:
        manager.add_ical_source(url=f["url"], name=f["name"])

    events = manager.get_events(start_dt, end_dt)
    tz_obj = ZoneInfo(await get_setting("timezone", "UTC"))
    return JSONResponse({
        "count": len(events),
        "events": [
            {
                "title": e.title,
                "start": e.start.astimezone(tz_obj).strftime("%Y-%m-%d %H:%M"),
                "end": e.end.astimezone(tz_obj).strftime("%Y-%m-%d %H:%M"),
                "source": e.source,
                "calendar": e.calendar_name,
            }
            for e in events
        ]
    })


@app.get("/api/history/count")
async def history_count():
    uploads = await get_uploads(limit=1)
    return JSONResponse({"count": len(uploads), "latest": uploads[0]["created_at"] if uploads else None})
