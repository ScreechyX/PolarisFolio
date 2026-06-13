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
from datetime import datetime, timezone, date, timedelta
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
    Parses raw ICS bytes and returns CalendarEvent objects within the
    given date range. Expands RRULE recurring events.

    Correctly handles Outlook ICS feeds which publish:
      - A master VEVENT with RRULE (the recurring series definition)
      - VEVENTs with RECURRENCE-ID (exception/modified instances)
      - Plain VEVENTs (non-recurring)

    We expand masters via RRULE and skip RECURRENCE-ID duplicates to
    avoid double-counting.
    """
    cal = Calendar.from_ical(ical_data)

    masters = {}           # uid -> component (has RRULE)
    exc_datetimes = {}     # uid -> [recurrence-id datetimes] for RRULE exclusion
    exc_components = {}    # uid -> [components] for orphan fallback
    singles = []           # plain non-recurring VEVENTs

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        uid = str(component.get("UID", ""))
        recurrence_id = component.get("RECURRENCE-ID")
        rrule = component.get("RRULE")

        if recurrence_id:
            # Modified/cancelled instance — track datetime for RRULE exclusion,
            # and keep the component in case there's no master (orphan instance)
            is_all_day = [False]
            exc_dt = _to_datetime(recurrence_id, is_all_day)
            if exc_dt:
                exc_datetimes.setdefault(uid, []).append(exc_dt)
            exc_components.setdefault(uid, []).append(component)
        elif rrule:
            masters[uid] = component
        else:
            singles.append(component)

    events = []

    # Expand recurring masters (excluding modified instances)
    for uid, component in masters.items():
        expanded = _expand_vevent(component, calendar_name, start_date, end_date,
                                  extra_exdates=exc_datetimes.get(uid, []))
        events.extend(expanded)

    # Orphaned exception instances (RECURRENCE-ID with no master in this feed window)
    # — parse them as regular events rather than discarding them
    for uid, components in exc_components.items():
        if uid not in masters:
            for component in components:
                parsed = _parse_vevent(component, calendar_name)
                if parsed is None:
                    continue
                if parsed.end < start_date or parsed.start > end_date:
                    continue
                events.append(parsed)

    # Parse non-recurring singles
    for component in singles:
        parsed = _parse_vevent(component, calendar_name)
        if parsed is None:
            continue
        if parsed.end < start_date or parsed.start > end_date:
            continue
        events.append(parsed)

    events.sort(key=lambda e: e.start)
    return events


def _expand_vevent(
    component,
    calendar_name: str,
    start_date: datetime,
    end_date: datetime,
    extra_exdates: list = None,
) -> list[CalendarEvent]:
    """
    Returns one CalendarEvent per occurrence of this VEVENT within the
    date range. Non-recurring events return a list of 0 or 1 items.
    Recurring events (RRULE) are expanded using dateutil.
    """
    rrule_raw = component.get("RRULE")
    if not rrule_raw:
        # Non-recurring: parse once and filter
        parsed = _parse_vevent(component, calendar_name)
        if parsed is None:
            return []
        if parsed.end < start_date or parsed.start > end_date:
            return []
        return [parsed]

    # Recurring event — expand instances
    is_all_day = [False]
    start_raw = component.get("DTSTART")
    end_raw = component.get("DTEND") or component.get("DURATION")

    master_start = _to_datetime(start_raw, is_all_day)
    if master_start is None:
        return []

    # Calculate event duration
    if end_raw and hasattr(end_raw, "dt"):
        if hasattr(end_raw.dt, "days") and not isinstance(end_raw.dt, datetime):
            # It's a timedelta (DURATION)
            duration = end_raw.dt
        else:
            master_end = _to_datetime(end_raw, [False])
            duration = (master_end - master_start) if master_end else timedelta(0)
    else:
        duration = timedelta(0)

    # Build rrule string and expand
    try:
        rrule_str = rrule_raw.to_ical().decode()
        rset = rruleset()
        rule = rrulestr(f"RRULE:{rrule_str}", dtstart=master_start, ignoretz=False)
        rset.rrule(rule)

        # Add EXDATEs (exception dates / cancelled instances)
        exdates = component.get("EXDATE")
        if exdates:
            if not isinstance(exdates, list):
                exdates = [exdates]
            for exdate_list in exdates:
                for exdt in exdate_list.dts:
                    dt = exdt.dt
                    if isinstance(dt, date) and not isinstance(dt, datetime):
                        dt = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
                    elif dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    rset.exdate(dt)

        # Add RECURRENCE-ID exception dates from modified instances
        for exc_dt in (extra_exdates or []):
            rset.exdate(exc_dt)

        uid = str(component.get("UID", ""))
        title = str(component.get("SUMMARY", "(No title)"))
        location_raw = component.get("LOCATION")
        location = str(location_raw) if location_raw else None
        description_raw = component.get("DESCRIPTION")
        description = _clean_description(str(description_raw) if description_raw else None)
        attendees = _parse_attendees(component)

        results = []
        for occurrence_start in rset.between(
            start_date - timedelta(days=1),
            end_date + timedelta(days=1),
            inc=True,
        ):
            if occurrence_start.tzinfo is None:
                occurrence_start = occurrence_start.replace(tzinfo=timezone.utc)
            occurrence_end = occurrence_start + duration

            if occurrence_end < start_date or occurrence_start > end_date:
                continue

            results.append(CalendarEvent(
                id=_stable_id(uid, occurrence_start),
                title=title,
                start=occurrence_start,
                end=occurrence_end,
                location=location,
                description=description,
                attendees=attendees,
                is_all_day=is_all_day[0],
                calendar_name=calendar_name,
                source="ical",
            ))
        return results

    except Exception as e:
        print(f"  Warning: failed to expand recurring event '{component.get('SUMMARY', '')}' - {e}")
        # Fall back to the master event start if expansion fails
        parsed = _parse_vevent(component, calendar_name)
        if parsed and start_date <= parsed.start <= end_date:
            return [parsed]
        return []


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
