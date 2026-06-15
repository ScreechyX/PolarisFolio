"""
Automated planner scheduler.

Runs inside the FastAPI process via APScheduler.
Reads schedule config from the settings table and generates + uploads
a planner on the configured schedule.

Settings keys used:
  schedule_enabled   - "1" or "0"
  schedule_day       - day of week: "mon", "tue", "wed", "thu", "fri", "sat", "sun"
  schedule_hour      - hour to run (0-23), e.g. "7"
  schedule_weeks_ahead - how many weeks forward to plan, e.g. "2"
  schedule_upload    - "1" to auto-upload to reMarkable, "0" to generate only
"""

import asyncio
from datetime import date, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from database import get_setting, add_upload

scheduler = AsyncIOScheduler()

DAY_MAP = {
    "mon": "mon", "tue": "tue", "wed": "wed", "thu": "thu",
    "fri": "fri", "sat": "sat", "sun": "sun",
}


async def _scheduled_generate():
    """The job function — same logic as app._run_generation."""
    from database import get_setting, get_ical_feeds
    from calendar_manager import CalendarManager
    from pdf_generator import build_planner
    from rm_uploader import RemarkableUploader
    import os
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    enabled = await get_setting("schedule_enabled", "0")
    if enabled != "1":
        return

    weeks_ahead = int(await get_setting("schedule_weeks_ahead", "2"))
    upload = (await get_setting("schedule_upload", "0")) == "1"
    rm_folder = await get_setting("rm_folder", "/PolarisFolio")
    tz_name = await get_setting("timezone", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz_name, tz = "UTC", ZoneInfo("UTC")

    start = datetime.now(tz).date()
    end = start + timedelta(weeks=weeks_ahead)

    manager = CalendarManager()

    # Try to get MS token without importing app to avoid circular import
    try:
        import msal, json
        TOKEN_CACHE_FILE = os.path.expanduser("~/.polarisfolio_msal_cache.json")
        MS_AUTHORITY = "https://login.microsoftonline.com/common"
        MS_SCOPES = ["Calendars.Read", "User.Read"]
        client_id = await get_setting("ms_client_id")
        if client_id and os.path.exists(TOKEN_CACHE_FILE):
            cache = msal.SerializableTokenCache()
            with open(TOKEN_CACHE_FILE) as f:
                cache.deserialize(f.read())
            ms_app = msal.PublicClientApplication(
                client_id, authority=MS_AUTHORITY, token_cache=cache
            )
            accounts = ms_app.get_accounts()
            if accounts:
                result = ms_app.acquire_token_silent(MS_SCOPES, account=accounts[0])
                if result and "access_token" in result:
                    manager.add_graph_source(result["access_token"])
    except Exception as e:
        print(f"Scheduler: MS token error - {e}")

    feeds = await get_ical_feeds(enabled_only=True)
    for f in feeds:
        manager.add_ical_source(url=f["url"], name=f["name"])

    if not manager._sources:
        print("Scheduler: no calendar sources configured, skipping")
        return

    start_dt = datetime.combine(start, datetime.min.time()).replace(tzinfo=tz)
    end_dt = datetime.combine(end, datetime.max.time()).replace(tzinfo=tz)
    events = manager.get_events(start_dt, end_dt)

    PDF_DIR = os.path.expanduser("~/polarisfolio_pdfs")
    os.makedirs(PDF_DIR, exist_ok=True)
    display_name = f"PolarisFolio {start.strftime('%b %Y')} (auto)"
    filename = f"polarisfolio_{start.isoformat()}_{end.isoformat()}_auto.pdf"
    pdf_path = os.path.join(PDF_DIR, filename)

    build_planner(
        events=events,
        output_path=pdf_path,
        start_date=start,
        end_date=end,
        title=display_name,
        timezone_name=tz_name,
    )

    uploaded = False
    if upload and os.path.exists(pdf_path):
        try:
            uploader = RemarkableUploader()
            uploaded = uploader.upload(display_name, pdf_path, folder=rm_folder)
        except Exception as e:
            print(f"Scheduler: upload error - {e}")

    await add_upload(
        display_name=display_name,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        event_count=len(events),
        pdf_path=pdf_path,
        uploaded_to_rm=uploaded,
    )
    print(f"Scheduler: generated {display_name} ({len(events)} events)")


def _make_trigger(day: str, hour: int) -> CronTrigger:
    return CronTrigger(day_of_week=DAY_MAP.get(day, "mon"), hour=hour, minute=0)


async def apply_schedule():
    """
    Reads schedule settings and updates the APScheduler job.
    Call this on startup and whenever settings are saved.
    """
    enabled = await get_setting("schedule_enabled", "0")
    day = await get_setting("schedule_day", "mon")
    hour = int(await get_setting("schedule_hour", "7"))

    scheduler.remove_all_jobs()

    if enabled == "1":
        scheduler.add_job(
            _scheduled_generate,
            trigger=_make_trigger(day, hour),
            id="auto_generate",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        print(f"Scheduler: auto-generate enabled — {day} at {hour:02d}:00")
    else:
        print("Scheduler: auto-generate disabled")
