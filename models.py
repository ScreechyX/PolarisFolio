"""
Unified event model - normalises events from both Microsoft Graph and ICS feeds
into a consistent structure for PDF generation.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Attendee:
    name: str
    email: str
    response: str = "unknown"  # accepted, declined, tentative, unknown


@dataclass
class CalendarEvent:
    id: str
    title: str
    start: datetime
    end: datetime
    location: Optional[str] = None
    description: Optional[str] = None
    attendees: list[Attendee] = field(default_factory=list)
    is_all_day: bool = False
    calendar_name: str = "Calendar"
    source: str = "unknown"  # "graph" or "ical"

    @property
    def duration_minutes(self) -> int:
        delta = self.end - self.start
        return int(delta.total_seconds() / 60)

    @property
    def duration_str(self) -> str:
        mins = self.duration_minutes
        if mins < 60:
            return f"{mins}m"
        hours = mins // 60
        remainder = mins % 60
        if remainder:
            return f"{hours}h {remainder}m"
        return f"{hours}h"

    @property
    def time_str(self) -> str:
        if self.is_all_day:
            return "All day"
        return f"{self.start.strftime('%I:%M %p')} - {self.end.strftime('%I:%M %p')}"
