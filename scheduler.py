"""
Automated planner scheduler.

Runs inside the FastAPI process via APScheduler.
Reads schedule config from the settings table and generates + uploads
a planner on the configured schedule.

Settings keys used:
  schedule_enabled   - "1" or "0"
  schedule_day       - "daily" (every day) or a weekday: "mon".."sun"
  schedule_hour      - hour to run (0-23), e.g. "7"
  schedule_weeks_ahead - how many weeks forward to plan, e.g. "2"
  schedule_upload    - "1" to auto-upload to reMarkable, "0" to generate only
  sync_mode          - "rolling" (one fixed yearly planner, updated in place so
                       handwriting is preserved) or "dated" (a new dated doc per
                       run, old ones pruned). Default "rolling".
  sync_meeting_slots - rolling mode: fixed number of per-meeting note pages
                       reserved in the yearly planner (default 200)
  schedule_keep_days - dated mode: how many recent dated docs to keep (default 5)

Run "daily" to keep the relative-date pills (TODAY/TOMORROW/THIS WEEK) correct:
a static PDF can't relabel itself, so it must be regenerated each morning.
"""

import asyncio
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from database import get_setting, add_upload

scheduler = AsyncIOScheduler()

DAY_MAP = {
    "mon": "mon", "tue": "tue", "wed": "wed", "thu": "thu",
    "fri": "fri", "sat": "sat", "sun": "sun",
}


async def _scheduled_generate():
    """The job function — pulls calendars, builds the planner, and syncs.

    Triggered by the daily schedule and by the Settings "Run now" button
    (POST /api/schedule/run-now).
    """
    from database import get_setting, get_ical_feeds
    from calendar_manager import CalendarManager
    from pdf_generator import build_planner
    from rm_uploader import RemarkableUploader
    import os
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    sync_mode = await get_setting("sync_mode", "rolling")

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

    import asyncio

    # Rolling mode: one fixed yearly planner, updated in place so handwriting
    # is preserved (see _run_rolling_sync). Default behaviour.
    if sync_mode == "rolling":
        await _run_rolling_sync(manager, tz, tz_name, rm_folder, upload)
        return

    # ── Dated mode: a fresh dated document each run, old ones pruned ───────────
    start_dt = datetime.combine(start, datetime.min.time()).replace(tzinfo=tz)
    end_dt = datetime.combine(end, datetime.max.time()).replace(tzinfo=tz)
    # Blocking network I/O — keep it off the shared event loop.
    events = await asyncio.to_thread(manager.get_events, start_dt, end_dt)

    PDF_DIR = os.path.expanduser("~/polarisfolio_pdfs")
    os.makedirs(PDF_DIR, exist_ok=True)
    # Dated name → one document per day on the reMarkable, so previous days'
    # handwritten notes are never overwritten.
    display_name = f"PolarisFolio {start.isoformat()}"
    filename = f"polarisfolio_{start.isoformat()}_{end.isoformat()}_auto.pdf"
    pdf_path = os.path.join(PDF_DIR, filename)

    # CPU-heavy reportlab work — run in a thread so the web server stays
    # responsive while a scheduled planner is generated.
    await asyncio.to_thread(
        build_planner,
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
            # force=False: never overwrite an existing dated doc (protect notes)
            uploaded = await asyncio.to_thread(
                uploader.upload, display_name, pdf_path, folder=rm_folder, force=False)
            # Keep only the most recent N dated planners on the device.
            keep_days = int(await get_setting("schedule_keep_days", "5"))
            await asyncio.to_thread(
                uploader.prune_old_dated, keep_days, rm_folder)
        except Exception as e:
            print(f"Scheduler: upload error - {e}")

    await add_upload(
        display_name=display_name,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        event_count=len(events),
        pdf_path=pdf_path,
        uploaded_to_rm=uploaded,
        sync_action="dated" if uploaded else None,
    )
    print(f"Scheduler: generated {display_name} ({len(events)} events)")


async def _run_rolling_sync(manager, tz, tz_name, rm_folder, upload):
    """
    Generate/refresh the single fixed-geometry yearly planner and sync it to the
    reMarkable *in place*, preserving existing handwriting.

    The whole calendar year is pulled so every page is current. Each meeting is
    given a permanent page slot (database.assign_meeting_slots), so its note page
    — and the ink on it — stays put even when the meeting is rescheduled. When
    the page geometry is unchanged from the live document we use
    `rmapi put --content-only` (keeps the doc ID + all annotations); when it
    changed (new year, slot count, week count or layout) we recreate the doc so
    annotations can't land on the wrong pages.
    """
    import os
    import asyncio
    from datetime import datetime
    from database import (get_setting, set_setting, assign_meeting_slots,
                          clear_meeting_slots, add_upload)
    from pdf_generator import (build_yearly_planner, yearly_geometry_signature,
                               event_qualifies_for_slot)
    from rm_uploader import RemarkableUploader

    year        = datetime.now(tz).year
    slot_count  = int(await get_setting("sync_meeting_slots", "200"))
    slot_filter = await get_setting("slot_filter", "attendees")
    doc_name    = (await get_setting("rm_doc_name", "")).strip() or f"PolarisFolio {year}"
    self_email  = (await get_setting("ms_user_email", "")).strip() or None

    # Decide in-place vs recreate up front (geometry depends only on year +
    # slot count). A recreate resets ink, so it's also the moment to re-slot
    # cleanly — wipe stale assignments so a changed filter/count starts fresh.
    sig          = yearly_geometry_signature(year, slot_count)
    live_sig     = await get_setting("sync_live_sig", "")
    content_only = (live_sig == sig)
    if not content_only and live_sig:
        await clear_meeting_slots(year)

    # Pull the full year so the fixed planner reflects the current calendar.
    year_start = datetime(year, 1, 1, 0, 0, 0, tzinfo=tz)
    year_end   = datetime(year, 12, 31, 23, 59, 59, tzinfo=tz)
    events = await asyncio.to_thread(manager.get_events, year_start, year_end)

    # Only meetings matching the slot filter get a permanent note page. Sorted
    # so first-time slot assignment is deterministic; existing slots never move.
    timed = [e for e in events if not e.is_all_day]
    qualifying = sorted(
        [e for e in timed if event_qualifies_for_slot(e, slot_filter, self_email)],
        key=lambda e: e.start)
    print(f"  Slot filter '{slot_filter}': {len(qualifying)} of {len(timed)} "
          f"timed events qualify for a note page")
    if slot_filter == "attendees" and timed and not qualifying:
        print("  WARNING: no events have attendees — your feed may not include "
              "ATTENDEE data. Set slot filter to 'all' in Settings if so.")
    slot_map = await assign_meeting_slots(year, qualifying, slot_count)

    PDF_DIR = os.path.expanduser("~/polarisfolio_pdfs")
    os.makedirs(PDF_DIR, exist_ok=True)
    pdf_path = os.path.join(PDF_DIR, f"polarisfolio_{year}_rolling.pdf")

    await asyncio.to_thread(
        build_yearly_planner,
        events=events, output_path=pdf_path, year=year,
        slot_map=slot_map, slot_count=slot_count,
        timezone_name=tz_name, self_email=self_email, title=doc_name)

    uploaded = False
    # Records what actually happened to the device doc, surfaced in History.
    sync_action = "generated"
    if upload and os.path.exists(pdf_path):
        try:
            uploader = RemarkableUploader()
            uploaded = await asyncio.to_thread(
                uploader.update_in_place, doc_name, pdf_path, rm_folder, content_only)
            if uploaded:
                # An unchanged geometry is swapped in place (ink preserved);
                # a changed one is recreated. Note the live signature now set.
                sync_action = "in_place" if content_only else "recreated"
                await set_setting("sync_live_sig", sig)
            else:
                sync_action = "upload_failed"
        except Exception as e:
            print(f"Scheduler: rolling sync error - {e}")
            sync_action = "upload_failed"

    await add_upload(
        display_name=doc_name,
        start_date=year_start.date().isoformat(),
        end_date=year_end.date().isoformat(),
        event_count=len(events),
        pdf_path=pdf_path,
        uploaded_to_rm=uploaded,
        sync_action=sync_action,
    )
    print(f"Scheduler: rolling sync {doc_name} ({len(events)} events, "
          f"{'updated in place' if content_only else 'recreated'})")


def _make_trigger(day: str, hour: int, tz=None) -> CronTrigger:
    # "daily" → every day; otherwise the given weekday.
    # tz pins the hour to the user's timezone setting, not the host clock
    # (the container runs UTC, so an unpinned hour fired 10h off in Brisbane).
    dow = "*" if day == "daily" else DAY_MAP.get(day, "mon")
    return CronTrigger(day_of_week=dow, hour=hour, minute=0, timezone=tz)


async def apply_schedule():
    """
    Reads schedule settings and updates the APScheduler job.
    Call this on startup and whenever settings are saved.
    """
    enabled = await get_setting("schedule_enabled", "0")
    day = await get_setting("schedule_day", "mon")
    hour = int(await get_setting("schedule_hour", "7"))

    # Fire the job in the user's timezone, not the host clock (UTC in prod).
    tz_name = await get_setting("timezone", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz_name, tz = "UTC", ZoneInfo("UTC")

    # Only touch our own job — leave the Claude-watch job (and any others) alone.
    try:
        scheduler.remove_job("auto_generate")
    except Exception:
        pass

    if enabled == "1":
        scheduler.add_job(
            _scheduled_generate,
            trigger=_make_trigger(day, hour, tz),
            id="auto_generate",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        print(f"Scheduler: auto-generate enabled — {day} at {hour:02d}:00 {tz_name}")
    else:
        print("Scheduler: auto-generate disabled")


async def apply_claude_watch():
    """
    Reads the Claude auto-watch settings and updates its interval job.
    Call this on startup and whenever settings are saved.

    The job (`_run_claude_watch`, defined in app.py) polls the Claude notebook
    every N minutes and answers its latest page only when it has changed.
    """
    enabled = await get_setting("claude_watch_enabled", "0")
    try:
        interval = max(1, int(await get_setting("claude_watch_interval", "5")))
    except (TypeError, ValueError):
        interval = 5

    try:
        scheduler.remove_job("claude_watch")
    except Exception:
        pass

    if enabled == "1":
        from app import _run_claude_watch  # lazy: app imports scheduler at top
        scheduler.add_job(
            _run_claude_watch,
            trigger=IntervalTrigger(minutes=interval),
            id="claude_watch",
            replace_existing=True,
            max_instances=1,       # never overlap polls
            coalesce=True,
            misfire_grace_time=300,
        )
        print(f"Scheduler: Claude watch enabled — every {interval} min")
    else:
        print("Scheduler: Claude watch disabled")
