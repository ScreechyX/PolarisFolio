"""
Calendar manager - combines events from Microsoft Graph and/or ICS feeds
into a single unified, deduplicated, sorted list.

Usage:

    manager = CalendarManager()

    # Add Microsoft 365 via OAuth
    manager.add_graph_source(token)

    # Add ICS feed (fallback or additional calendar)
    manager.add_ical_source(
        url="https://outlook.office365.com/owa/calendar/.../reachcalendar.ics",
        name="Work Calendar"
    )

    # Get all events for a date range
    events = manager.get_events(start_date, end_date)
"""

from datetime import datetime, timezone
from typing import Optional

from models import CalendarEvent
from graph_connector import get_events as graph_get_events
from ical_connector import get_events_from_url


class CalendarSource:
    def __init__(self, source_type: str, name: str, **kwargs):
        self.source_type = source_type  # "graph" or "ical"
        self.name = name
        self.kwargs = kwargs


class CalendarManager:
    def __init__(self):
        self._sources: list[CalendarSource] = []

    def add_graph_source(
        self,
        token: str,
        calendar_id: Optional[str] = None,
        name: str = "Microsoft 365",
    ):
        """Add a Microsoft Graph calendar source (OAuth)."""
        self._sources.append(CalendarSource(
            source_type="graph",
            name=name,
            token=token,
            calendar_id=calendar_id,
        ))
        print(f"  + Added Microsoft Graph source: {name}")

    def add_ical_source(self, url: str, name: str = "iCal Feed"):
        """Add an ICS feed source."""
        self._sources.append(CalendarSource(
            source_type="ical",
            name=name,
            url=url,
        ))
        print(f"  + Added iCal source: {name}")

    def get_events(
        self,
        start_date: datetime,
        end_date: datetime,
        deduplicate: bool = True,
    ) -> list[CalendarEvent]:
        """
        Fetches and merges events from all sources.
        Deduplication removes events with the same title and start time
        (useful when the same calendar is connected via both Graph and ICS).
        """
        all_events: list[CalendarEvent] = []

        for source in self._sources:
            try:
                print(f"  Fetching from {source.name}...")
                if source.source_type == "graph":
                    events = graph_get_events(
                        token=source.kwargs["token"],
                        start_date=start_date,
                        end_date=end_date,
                        calendar_id=source.kwargs.get("calendar_id"),
                    )
                elif source.source_type == "ical":
                    events = get_events_from_url(
                        url=source.kwargs["url"],
                        start_date=start_date,
                        end_date=end_date,
                        calendar_name=source.name,
                    )
                else:
                    continue

                print(f"    {len(events)} events found")
                all_events.extend(events)

            except Exception as e:
                print(f"  Warning: failed to fetch from {source.name} - {e}")

        if deduplicate:
            all_events = self._deduplicate(all_events)

        # Sort by start time
        all_events.sort(key=lambda e: e.start)
        return all_events

    def _deduplicate(self, events: list[CalendarEvent]) -> list[CalendarEvent]:
        """
        Removes duplicate events based on title + start time fingerprint.
        Prefers Graph source over iCal when duplicates exist (Graph has richer data).
        """
        seen: dict[str, CalendarEvent] = {}

        for event in events:
            key = f"{event.title.lower().strip()}_{event.start.isoformat()}"
            if key not in seen:
                seen[key] = event
            else:
                # Prefer Graph source (has attendee RSVP status)
                if event.source == "graph":
                    seen[key] = event

        return list(seen.values())

    def group_by_day(
        self,
        events: list[CalendarEvent],
    ) -> dict[str, list[CalendarEvent]]:
        """
        Groups events by date string (YYYY-MM-DD).
        Useful for building daily planner pages.
        """
        grouped: dict[str, list[CalendarEvent]] = {}
        for event in events:
            day_key = event.start.strftime("%Y-%m-%d")
            if day_key not in grouped:
                grouped[day_key] = []
            grouped[day_key].append(event)
        return grouped


# -- Quick test --------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from datetime import timedelta

    print("Calendar Manager - Combined Source Test")
    print("=" * 40)

    manager = CalendarManager()

    # Test with a public ICS feed (no auth required)
    test_url = "https://www.officeholidays.com/ics/australia/queensland"
    manager.add_ical_source(url=test_url, name="QLD Public Holidays")

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=90)

    print(f"\nFetching events from {now.strftime('%d %b')} to {end.strftime('%d %b %Y')}...")
    events = manager.get_events(now, end)

    print(f"\nTotal events: {len(events)}\n")
    grouped = manager.group_by_day(events)

    for day, day_events in sorted(grouped.items()):
        print(f"{day}:")
        for e in day_events:
            print(f"  {e.time_str:20} {e.title} [{e.source}]")
