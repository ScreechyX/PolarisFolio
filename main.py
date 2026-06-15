"""
PolarisFolio Clone - Main entry point.

Wires together:
  1. Calendar pull (Microsoft Graph OAuth and/or ICS feed)
  2. PDF generation (monthly, daily, meeting note pages)
  3. reMarkable cloud upload

Usage:
  # First-time setup (REST API fallback only - skip if using rmapi)
  python main.py setup <8-letter-code>

  # Generate and upload planner for the next 2 weeks
  python main.py run

  # Generate PDF only, no upload
  python main.py run --no-upload

  # Generate for a specific date range
  python main.py run --start 2026-06-16 --end 2026-06-30

  # Use ICS feed only (no Microsoft OAuth)
  python main.py run --ics "https://outlook.office365.com/owa/..."

  # Use Microsoft OAuth only (no ICS)
  python main.py run --graph-only
"""

import os
import sys
import argparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from calendar_manager import CalendarManager
from pdf_generator import build_planner
from rm_uploader import RemarkableUploader

# ---------------------------------------------------------------------------
# Config - edit these defaults or pass as CLI args
# ---------------------------------------------------------------------------

# How many days ahead to generate the planner for
DEFAULT_DAYS_AHEAD = 14

# Timezone used to place events and date the planner (IANA name, e.g.
# "Australia/Brisbane"). Override per-run with --tz.
TIMEZONE = "UTC"

# Folder on the reMarkable to upload into
RM_FOLDER = "/PolarisFolio"

# Display name on the reMarkable device
RM_DISPLAY_NAME = "PolarisFolio Planner"

# Local output path for the generated PDF
PDF_OUTPUT = os.path.expanduser("~/polarisfolio_planner.pdf")

# ICS feed URLs - add yours here or pass via --ics flag
# Example: your Outlook calendar ICS URL
ICS_FEEDS: list[tuple[str, str]] = [
    # ("Calendar Name", "https://your-ics-url-here"),
]

# Microsoft Graph - set MS_CLIENT_ID env var from your Azure app registration
# Leave MS_GRAPH_ENABLED = False if you're using ICS only
MS_GRAPH_ENABLED = bool(os.environ.get("MS_CLIENT_ID"))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_setup(args):
    """Register with reMarkable cloud via one-time code."""
    RemarkableUploader.setup(args.code)


def cmd_run(args):
    from zoneinfo import ZoneInfo
    tz_name = getattr(args, "tz", None) or TIMEZONE
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz_name, tz = "UTC", ZoneInfo("UTC")

    # Date range (today in the configured timezone)
    start = datetime.now(tz).date()
    end = start + timedelta(days=DEFAULT_DAYS_AHEAD)

    if args.start:
        start = datetime.strptime(args.start, "%Y-%m-%d").date()
    if args.end:
        end = datetime.strptime(args.end, "%Y-%m-%d").date()

    print(f"Generating planner: {start.strftime('%-d %b')} to {end.strftime('%-d %b %Y')}")
    print()

    # --- Calendar pull ---
    manager = CalendarManager()

    if MS_GRAPH_ENABLED and not args.ics_only:
        try:
            from graph_connector import authenticate
            print("Authenticating with Microsoft 365...")
            token = authenticate()
            manager.add_graph_source(token, name="Microsoft 365")
        except Exception as e:
            print(f"  Microsoft Graph failed: {e}")
            print("  Falling back to ICS feeds only.")

    # ICS feeds from config
    for name, url in ICS_FEEDS:
        manager.add_ical_source(url=url, name=name)

    # ICS feeds from CLI
    if args.ics:
        for i, url in enumerate(args.ics):
            manager.add_ical_source(url=url, name=f"Calendar {i + 1}")

    if not manager._sources:
        print("No calendar sources configured.")
        print()
        print("Options:")
        print("  1. Set MS_CLIENT_ID env var and ensure Calendars.Read permission")
        print("     to use Microsoft Graph OAuth")
        print("  2. Pass --ics <url> with your Outlook ICS feed URL")
        print("  3. Add ICS_FEEDS to main.py directly")
        sys.exit(1)

    print()
    print("Fetching calendar events...")
    start_dt = datetime.combine(start, datetime.min.time()).replace(tzinfo=tz)
    end_dt = datetime.combine(end, datetime.max.time()).replace(tzinfo=tz)
    events = manager.get_events(start_dt, end_dt)

    print(f"\nTotal events: {len(events)}")
    if events:
        for e in events[:5]:
            print(f"  {e.start.strftime('%a %-d %b')} {e.time_str} - {e.title}")
        if len(events) > 5:
            print(f"  ... and {len(events) - 5} more")

    # --- PDF generation ---
    print(f"\nGenerating PDF...")
    output_path = args.output or PDF_OUTPUT

    build_planner(
        events=events,
        output_path=output_path,
        start_date=start,
        end_date=end,
        title=f"PolarisFolio {start.strftime('%B %Y')}",
        timezone_name=tz_name,
    )

    size_kb = os.path.getsize(output_path) / 1024
    print(f"PDF written: {output_path} ({size_kb:.0f} KB)")

    # --- Upload ---
    if args.no_upload:
        print("\nSkipping upload (--no-upload)")
        return

    print()
    uploader = RemarkableUploader()
    display_name = f"PolarisFolio {start.strftime('%b %Y')}"
    success = uploader.upload(
        display_name=display_name,
        pdf_path=output_path,
        folder=RM_FOLDER,
    )

    if success:
        print(f"\nDone. '{display_name}' will appear on your reMarkable shortly.")
    else:
        print("\nUpload failed. Check the PDF locally at:", output_path)
        print("You can manually import it via the reMarkable app or USB.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="PolarisFolio Clone - reMarkable planner from your calendar"
    )
    subparsers = parser.add_subparsers(dest="command")

    # setup
    setup_parser = subparsers.add_parser("setup", help="Register with reMarkable cloud")
    setup_parser.add_argument("code", help="8-letter code from my.remarkable.com/device/desktop/connect")

    # run
    run_parser = subparsers.add_parser("run", help="Generate and upload planner")
    run_parser.add_argument("--start", help="Start date (YYYY-MM-DD), default: today")
    run_parser.add_argument("--end", help="End date (YYYY-MM-DD), default: +14 days")
    run_parser.add_argument("--ics", action="append", metavar="URL",
                            help="ICS feed URL (can be passed multiple times)")
    run_parser.add_argument("--ics-only", action="store_true",
                            help="Skip Microsoft Graph, use ICS only")
    run_parser.add_argument("--graph-only", action="store_true",
                            help="Skip ICS feeds, use Microsoft Graph only")
    run_parser.add_argument("--no-upload", action="store_true",
                            help="Generate PDF but do not upload to reMarkable")
    run_parser.add_argument("--output", metavar="PATH",
                            help=f"Output PDF path (default: {PDF_OUTPUT})")
    run_parser.add_argument("--tz", metavar="ZONE",
                            help=f"Timezone (IANA name, default: {TIMEZONE})")

    args = parser.parse_args()

    if args.command == "setup":
        cmd_setup(args)
    elif args.command == "run":
        cmd_run(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
