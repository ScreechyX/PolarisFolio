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
  GET  /generate          - generate planner form
  POST /generate          - run generation + upload
  GET  /history           - past planners
  GET  /download/{id}     - download a generated PDF
  GET  /settings          - app settings
  POST /settings          - save settings
"""

import os
import shutil
import uuid
import asyncio
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
    add_upload, get_uploads, clear_uploads, update_upload_rm_status,
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

    # Upcoming events preview (next 24h) if MS connected
    upcoming = []
    if ms_ok:
        try:
            from graph_connector import get_events
            token = await get_ms_token()
            now = datetime.now(timezone.utc)
            events = get_events(token, now, now + timedelta(hours=24))
            upcoming = events[:5]
        except Exception:
            pass

    return templates.TemplateResponse(request, "dashboard.html", {
        "ms_connected": ms_ok,
        "feed_count": len([f for f in feeds if f["enabled"]]),
        "uploads": uploads,
        "upcoming": upcoming,
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
        # Save display name
        name = result.get("id_token_claims", {}).get("name", "")
        if name:
            await set_setting("ms_user_name", name)
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
# Generate
# ---------------------------------------------------------------------------

@app.get("/generate", response_class=HTMLResponse)
async def generate_page(request: Request, success: str = None, error: str = None):
    ms_ok = await ms_connected()
    feeds = await get_ical_feeds(enabled_only=True)
    has_sources = ms_ok or len(feeds) > 0

    today = date.today()
    default_end = today + timedelta(days=14)

    return templates.TemplateResponse(request, "generate.html", {
        "ms_connected": ms_ok,
        "feeds": feeds,
        "has_sources": has_sources,
        "default_start": today.isoformat(),
        "default_end": default_end.isoformat(),
        "success": success,
        "error": error,
        "active": "generate",
    })


@app.post("/generate")
async def run_generate(
    background_tasks: BackgroundTasks,
    start_date: str = Form(...),
    end_date: str = Form(...),
    upload_to_rm: str = Form("off"),
    rm_folder: str = Form("/PolarisFolio"),
):
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError:
        return RedirectResponse("/generate?error=invalid_dates", status_code=303)

    if end < start:
        return RedirectResponse("/generate?error=end_before_start", status_code=303)

    # Run generation in background, redirect to history
    background_tasks.add_task(
        _run_generation,
        start, end,
        upload_to_rm == "on",
        rm_folder,
    )

    return RedirectResponse("/history?generating=1", status_code=303)


async def _run_generation(
    start: date,
    end: date,
    upload: bool,
    rm_folder: str,
):
    """Background task: pull calendar, generate PDF, optionally upload."""
    from calendar_manager import CalendarManager
    from pdf_generator import build_planner
    from rm_uploader import RemarkableUploader

    manager = CalendarManager()

    # Microsoft Graph
    token = await get_ms_token()
    if token:
        manager.add_graph_source(token, name="Microsoft 365")

    # ICS feeds
    feeds = await get_ical_feeds(enabled_only=True)
    for f in feeds:
        manager.add_ical_source(url=f["url"], name=f["name"])

    if not manager._sources:
        return

    tz_name = await get_setting("timezone", "UTC")
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    start_dt = datetime(start.year, start.month, start.day, 0, 0, 0, tzinfo=tz)
    end_dt = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=tz)

    events = manager.get_events(start_dt, end_dt)

    # Generate PDF
    display_name = f"PolarisFolio {start.strftime('%b %Y')}"
    filename = f"polarisfolio_{start.isoformat()}_{end.isoformat()}.pdf"
    pdf_path = os.path.join(PDF_DIR, filename)
    build_planner(
        events=events,
        output_path=pdf_path,
        start_date=start,
        end_date=end,
        title=display_name,
        timezone_name=tz_name,
    )

    # Save to history immediately so the UI stops spinning
    upload_id = await add_upload(
        display_name=display_name,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        event_count=len(events),
        pdf_path=pdf_path,
        uploaded_to_rm=False,
    )

    # Upload (may take a while — history entry already visible)
    if upload and os.path.exists(pdf_path):
        try:
            uploader = RemarkableUploader({"rm_folder": rm_folder})
            uploaded = uploader.upload(display_name, pdf_path, folder=rm_folder)
            if uploaded:
                await update_upload_rm_status(upload_id, True)
        except Exception as e:
            print(f"Upload error: {e}")


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request, generating: str = None):
    uploads = await get_uploads(limit=50)
    return templates.TemplateResponse(request, "history.html", {
        "uploads": uploads,
        "generating": generating == "1",
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

    return templates.TemplateResponse(request, "settings.html", {
        "settings": settings,
        "ms_connected": ms_ok,
        "rmapi_ok": shutil.which("rmapi") is not None,
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

    await apply_schedule()

    return RedirectResponse("/settings?success=saved", status_code=303)


# ---------------------------------------------------------------------------
# API - event preview (for generate page)
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


@app.get("/api/events/count")
async def event_count(start: str, end: str):
    """Returns event count for a date range - used by the generate form."""
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except ValueError:
        return JSONResponse({"count": 0})

    from calendar_manager import CalendarManager
    manager = CalendarManager()

    token = await get_ms_token()
    if token:
        manager.add_graph_source(token)

    feeds = await get_ical_feeds(enabled_only=True)
    for f in feeds:
        manager.add_ical_source(url=f["url"], name=f["name"])

    if not manager._sources:
        return JSONResponse({"count": 0, "error": "no_sources"})

    from zoneinfo import ZoneInfo
    tz = ZoneInfo(await get_setting("timezone", "UTC"))
    start_dt = datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0, tzinfo=tz)
    end_dt = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=tz)

    try:
        events = manager.get_events(start_dt, end_dt)
        return JSONResponse({"count": len(events)})
    except Exception as e:
        return JSONResponse({"count": 0, "error": str(e)})
