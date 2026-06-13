"""
ICS / iCal feed connector.

Fetches a calendar ICS URL (e.g. from Outlook's "publish to internet" option,
or any other calendar app) and parses it into CalendarEvent objects.

How to get your Outlook ICS URL:
  1. Go to outlook.office.com
  2. Settings > View all Outlook settings > Calendar > Shared calendars
  3. Under "Publish a calendar", select your calendar and "Can view all details"
  4. Click Publish and copy the ICS link

Limitations of ICS feeds from M365:
  - Outlook ICS feeds typically only include events 3 months past to ~6 months future
  - Corporate/organisational tenants may block ICS publishing by default
    (IT admin needs to enable it in Exchange/M365 admin centre)
  - No RSVP/attendee response status in ICS feeds
"""

import hashlib
import re
from datetime import datetime, timezone, date
from typing import Optional
import requests
from icalendar import Calendar, Event
from dateutil.rrule import rruleset, rrulestr
from dateutil.relativedelta import relativedelta

from models import CalendarEvent, Attendee


# -- Fetch and parse ---------------------------------------------------------

def fetch_ical(url: str, timeout: int = 15) -> bytes:
    """Fetches raw ICS data from a URL."""
    headers = {
        "User-Agent": "PolarisFolioClone/1.0",
        "Accept": "text/calendar, application/calendar+json",
    }
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.content


def parse_ical(
    ical_data: bytes,
    start_date: datetime,
    end_date: datetime,
    calendar_name: str = "iCal Feed",
) -> list[CalendarEvent]:
    """
    Parses raw ICS bytes and returns CalendarEvent objects
    within the given date range.
    """
    cal = Calendar.from_ical(ical_data)
    events = []

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        parsed = _parse_vevent(component, calendar_name)
        if parsed is None:
            continue

        # Filter to date range
        if parsed.end < start_date or parsed.start > end_date:
            continue

        events.append(parsed)

    # Sort by start time
    events.sort(key=lambda e: e.start)
    return events


def get_events_from_url(
    url: str,
    start_date: datetime,
    end_date: datetime,
    calendar_name: str = "iCal Feed",
) -> list[CalendarEvent]:
    """Convenience function - fetch and parse in one call."""
    data = fetch_ical(url)
    return parse_ical(data, start_date, end_date, calendar_name)


# -- Parsing helpers ---------------------------------------------------------

def _to_datetime(value, is_all_day_ref: list) -> Optional[datetime]:
    """
    Converts icalendar date or datetime to a timezone-aware datetime.
    Sets is_all_day_ref[0] = True if it's a date-only value.
    """
    if value is None:
        return None

    # vDate (all-day events) vs vDatetime
    if isinstance(value.dt, date) and not isinstance(value.dt, datetime):
        is_all_day_ref[0] = True
        return datetime(
            value.dt.year, value.dt.month, value.dt.day,
            tzinfo=timezone.utc
        )

    dt = value.dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_attendees(component) -> list[Attendee]:
    """Extracts attendees from a VEVENT component."""
    attendees = []
    raw_attendees = component.get("ATTENDEE")

    if raw_attendees is None:
        return attendees

    # May be a single value or a list
    if not isinstance(raw_attendees, list):
        raw_attendees = [raw_attendees]

    for att in raw_attendees:
        params = att.params if hasattr(att, "params") else {}
        name = params.get("CN", "")
        email = str(att).replace("mailto:", "").replace("MAILTO:", "").strip()
        # ICS feeds don't include RSVP status reliably
        attendees.append(Attendee(name=name, email=email, response="unknown"))

    return attendees


def _stable_id(uid: str, start: datetime) -> str:
    """Generates a stable ID from UID + start time (handles recurring events)."""
    raw = f"{uid}_{start.isoformat()}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _clean_description(desc: Optional[str]) -> Optional[str]:
    """Strips HTML tags and excess whitespace from descriptions."""
    if not desc:
        return None
    # Remove HTML tags
    clean = re.sub(r"<[^>]+>", " ", desc)
    # Collapse whitespace
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean or None


def _parse_vevent(component, calendar_name: str) -> Optional[CalendarEvent]:
    """Converts a VEVENT icalendar component into a CalendarEvent."""
    try:
        is_all_day = [False]

        start_raw = component.get("DTSTART")
        end_raw = component.get("DTEND") or component.get("DURATION")

        start = _to_datetime(start_raw, is_all_day)
        if start is None:
            return None

        # Handle DURATION instead of DTEND
        if end_raw and hasattr(end_raw, "dt") and hasattr(end_raw.dt, "days"):
            # It's a duration
            from datetime import timedelta
            end = start + end_raw.dt
        else:
            end = _to_datetime(end_raw, is_all_day)

        if end is None:
            end = start

        uid = str(component.get("UID", ""))
        title = str(component.get("SUMMARY", "(No title)"))
        location_raw = component.get("LOCATION")
        location = str(location_raw) if location_raw else None
        description_raw = component.get("DESCRIPTION")
        description = _clean_description(str(description_raw) if description_raw else None)
        attendees = _parse_attendees(component)

        return CalendarEvent(
            id=_stable_id(uid, start),
            title=title,
            start=start,
            end=end,
            location=location,
            description=description,
            attendees=attendees,
            is_all_day=is_all_day[0],
            calendar_name=calendar_name,
            source="ical",
        )

    except Exception as e:
        # Skip malformed events rather than crashing
        print(f"  Warning: skipping malformed event - {e}")
        return None


# -- Quick test --------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from datetime import timedelta

    if len(sys.argv) < 2:
        print("Usage: python ical_connector.py <ICS_URL>")
        print()
        print("Example ICS URLs for testing:")
        print("  Australian public holidays:")
        print("  https://www.officeholidays.com/ics/australia")
        sys.exit(1)

    url = sys.argv[1]
    now = datetime.now(timezone.utc)

    print(f"Fetching: {url}")
    events = get_events_from_url(url, now, now + timedelta(days=30))

    print(f"\nFound {len(events)} events in the next 30 days:\n")
    for e in events:
        print(f"  {e.start.strftime('%a %d %b')} {e.time_str} - {e.title}")
        if e.location:
            print(f"    Location: {e.location}")
        if e.attendees:
            print(f"    Attendees: {', '.join(a.name or a.email for a in e.attendees)}")
