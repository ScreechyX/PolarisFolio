"""
PDF generator for the PolarisFolio-style planner.

Produces a hyperlinked PDF with:
  - Monthly overview page (tap a day to jump to its daily page)
  - Daily pages with timed schedule slots (tap an event to jump to its note page)
  - Per-event meeting note pages with metadata + lined writing area

Optimised for reMarkable Paper Pro (A4 portrait, clean minimal style).
"""

import os
import calendar
from datetime import datetime, date, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm

from models import CalendarEvent

# ---------------------------------------------------------------------------
# Page dimensions (A4 = 210 x 297 mm)
# ---------------------------------------------------------------------------
PAGE_W, PAGE_H = A4
MARGIN = 14 * mm
CONTENT_W = PAGE_W - 2 * MARGIN

# ---------------------------------------------------------------------------
# Colour palette (minimal, reMarkable-friendly)
# ---------------------------------------------------------------------------
C_BLACK      = colors.HexColor("#1A1A1A")
C_DARK_GREY  = colors.HexColor("#444444")
C_MID_GREY   = colors.HexColor("#888888")
C_LIGHT_GREY = colors.HexColor("#CCCCCC")
C_RULE       = colors.HexColor("#E8E8E8")
C_ACCENT     = colors.HexColor("#5C6BC0")   # indigo - tap targets / event blocks
C_ACCENT_BG  = colors.HexColor("#EEF0FB")   # light indigo for event chips
C_WHITE      = colors.white

# ---------------------------------------------------------------------------
# Typography helpers
# ---------------------------------------------------------------------------

def set_font(c: canvas.Canvas, size: float, bold: bool = False, grey: bool = False):
    font = "Helvetica-Bold" if bold else "Helvetica"
    c.setFont(font, size)
    c.setFillColor(C_DARK_GREY if grey else C_BLACK)


def draw_text(c: canvas.Canvas, x: float, y: float, text: str,
              size: float = 9, bold: bool = False,
              color=None, align: str = "left", max_width: float = None):
    """Draw text with optional truncation and alignment."""
    font = "Helvetica-Bold" if bold else "Helvetica"
    c.setFont(font, size)
    c.setFillColor(color or C_BLACK)

    if max_width and c.stringWidth(text, font, size) > max_width:
        while text and c.stringWidth(text + "…", font, size) > max_width:
            text = text[:-1]
        text = text + "…"

    if align == "right":
        c.drawRightString(x, y, text)
    elif align == "center":
        c.drawCentredString(x, y, text)
    else:
        c.drawString(x, y, text)


def _time_str(event, tz: ZoneInfo) -> str:
    """Returns a display time string for an event in the given timezone."""
    if event.is_all_day:
        return "All day"
    start = event.start.astimezone(tz)
    end = event.end.astimezone(tz)
    return f"{start.strftime('%I:%M %p')} - {end.strftime('%I:%M %p')}"


def draw_rule(c: canvas.Canvas, x: float, y: float, width: float,
              color=None, thickness: float = 0.4):
    c.setStrokeColor(color or C_RULE)
    c.setLineWidth(thickness)
    c.line(x, y, x + width, y)


def draw_rect(c: canvas.Canvas, x: float, y: float, w: float, h: float,
              fill_color=None, stroke_color=None, radius: float = 2):
    c.setFillColor(fill_color or C_WHITE)
    c.setStrokeColor(stroke_color or C_LIGHT_GREY)
    c.setLineWidth(0.5)
    c.roundRect(x, y, w, h, radius,
                fill=1 if fill_color else 0,
                stroke=1 if stroke_color else 0)


# ---------------------------------------------------------------------------
# Page header (shared across all page types)
# ---------------------------------------------------------------------------

def draw_header(c: canvas.Canvas, title: str, subtitle: str = "",
                page_label: str = ""):
    y = PAGE_H - MARGIN - 6 * mm

    draw_text(c, MARGIN, y, title, size=15, bold=True)

    if subtitle:
        draw_text(c, MARGIN, y - 6 * mm, subtitle, size=9, color=C_MID_GREY)

    if page_label:
        draw_text(c, PAGE_W - MARGIN, y, page_label,
                  size=8, color=C_MID_GREY, align="right")

    rule_y = PAGE_H - MARGIN - 14 * mm
    draw_rule(c, MARGIN, rule_y, CONTENT_W, color=C_LIGHT_GREY, thickness=0.6)

    return rule_y - 4 * mm   # return y to start content below header


# ---------------------------------------------------------------------------
# Monthly overview page
# ---------------------------------------------------------------------------

def draw_month_page(c: canvas.Canvas, year: int, month: int,
                    events: list[CalendarEvent],
                    day_page_map: dict[str, int]):
    """
    Draws a monthly calendar grid. Each day cell is a tap target
    linking to the corresponding daily page.
    """
    month_name = datetime(year, month, 1).strftime("%B %Y")
    content_y = draw_header(c, month_name, "Monthly Overview")

    # Group events by day number
    events_by_day: dict[int, list[CalendarEvent]] = {}
    for e in events:
        d = e.start.day
        if e.start.month == month and e.start.year == year:
            events_by_day.setdefault(d, []).append(e)

    # Calendar grid
    cal = calendar.monthcalendar(year, month)
    num_weeks = len(cal)

    grid_top = content_y - 8 * mm
    grid_h = grid_top - MARGIN - 4 * mm
    cell_w = CONTENT_W / 7
    cell_h = grid_h / num_weeks

    # Day-of-week headers
    dow_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    header_h = 7 * mm
    for col, label in enumerate(dow_labels):
        x = MARGIN + col * cell_w
        is_weekend = col >= 5
        draw_text(c, x + cell_w / 2, grid_top - 4 * mm, label,
                  size=7, bold=True,
                  color=C_MID_GREY if is_weekend else C_DARK_GREY,
                  align="center")

    cell_top = grid_top - header_h
    today = date.today()

    for row, week in enumerate(cal):
        for col, day_num in enumerate(week):
            if day_num == 0:
                continue

            x = MARGIN + col * cell_w
            y = cell_top - (row + 1) * cell_h
            is_today = (date(year, month, day_num) == today)
            is_weekend = col >= 5
            day_key = f"{year}-{month:02d}-{day_num:02d}"
            has_events = day_num in events_by_day

            # Cell background
            if is_today:
                draw_rect(c, x + 1, y + 1, cell_w - 2, cell_h - 2,
                          fill_color=C_ACCENT_BG, stroke_color=C_ACCENT, radius=3)

            # Day number
            num_color = C_ACCENT if is_today else (C_MID_GREY if is_weekend else C_BLACK)
            draw_text(c, x + 3 * mm, y + cell_h - 5 * mm, str(day_num),
                      size=9, bold=is_today, color=num_color)

            # Event count dot
            if has_events:
                count = len(events_by_day[day_num])
                dot_x = x + cell_w - 5 * mm
                dot_y = y + cell_h - 4 * mm
                c.setFillColor(C_ACCENT)
                c.circle(dot_x, dot_y, 2, fill=1, stroke=0)

                # Show first event title (truncated)
                first = events_by_day[day_num][0]
                draw_text(c, x + 2 * mm, y + cell_h - 9 * mm,
                          first.title, size=5.5, color=C_DARK_GREY,
                          max_width=cell_w - 4 * mm)

            # Tap target -> daily page
            if day_key in day_page_map:
                page_num = day_page_map[day_key]
                c.linkAbsolute(
                    "",
                    f"day_{day_key}",
                    (x + 1, y + 1, x + cell_w - 1, y + cell_h - 1),
                )

            # Cell border
            c.setStrokeColor(C_RULE)
            c.setLineWidth(0.3)
            c.rect(x, y, cell_w, cell_h, fill=0, stroke=1)


# ---------------------------------------------------------------------------
# Daily page
# ---------------------------------------------------------------------------

HOUR_START = 7    # 7am
HOUR_END   = 19   # 7pm
HOURS      = HOUR_END - HOUR_START

def draw_day_page(c: canvas.Canvas, day: date,
                  events: list[CalendarEvent],
                  event_page_map: dict[str, int],
                  month_page_num: int,
                  tz: ZoneInfo = None):
    """
    Draws a daily schedule page with hourly time slots.
    Each event block is a tap target linking to its meeting note page.
    """
    tz = tz or timezone.utc
    day_label = day.strftime("%A")
    date_label = day.strftime("%-d %B %Y")
    month_back = day.strftime("%B %Y")

    content_y = draw_header(c, day_label, date_label)

    # Back link to monthly page
    c.setFont("Helvetica", 7)
    c.setFillColor(C_ACCENT)
    back_x = PAGE_W - MARGIN
    back_y = PAGE_H - MARGIN - 6 * mm
    c.drawRightString(back_x, back_y, f"< {month_back}")
    c.linkAbsolute("", f"month_{day.year}_{day.month:02d}",
                   (back_x - 30 * mm, back_y - 2 * mm, back_x, back_y + 4 * mm))

    # Time grid
    grid_top = content_y - 2 * mm
    grid_bottom = MARGIN + 4 * mm
    grid_h = grid_top - grid_bottom
    slot_h = grid_h / HOURS

    time_col_w = 12 * mm
    event_col_x = MARGIN + time_col_w
    event_col_w = CONTENT_W - time_col_w

    # Hour rows
    for i in range(HOURS + 1):
        hour = HOUR_START + i
        y = grid_top - i * slot_h

        # Hour label
        label = f"{hour:02d}:00"
        draw_text(c, MARGIN, y - 1 * mm, label, size=7, color=C_MID_GREY)

        # Rule
        draw_rule(c, event_col_x, y, event_col_w,
                  color=C_RULE if hour % 2 == 0 else C_RULE,
                  thickness=0.4 if hour % 2 == 0 else 0.2)

    # All-day events strip
    all_day = [e for e in events if e.is_all_day]
    timed = [e for e in events if not e.is_all_day]

    if all_day:
        strip_y = grid_top + 2 * mm
        for i, e in enumerate(all_day[:3]):
            ex = event_col_x + i * (event_col_w / 3)
            ew = event_col_w / 3 - 1 * mm
            draw_rect(c, ex, strip_y - 4 * mm, ew, 4 * mm,
                      fill_color=C_ACCENT_BG, stroke_color=C_ACCENT)
            draw_text(c, ex + 2 * mm, strip_y - 3 * mm,
                      e.title, size=6, color=C_ACCENT,
                      max_width=ew - 4 * mm)

    # Timed event blocks
    for event in timed:
        local_start = event.start.astimezone(tz)
        local_end = event.end.astimezone(tz)
        if local_start.hour < HOUR_START or local_start.hour >= HOUR_END:
            continue

        start_offset = (local_start.hour - HOUR_START) + local_start.minute / 60
        end_hour = min(local_end.hour + local_end.minute / 60, HOUR_END)
        end_offset = end_hour - HOUR_START
        duration_slots = end_offset - start_offset

        ey = grid_top - end_offset * slot_h
        eh = duration_slots * slot_h - 1

        # Event block
        draw_rect(c, event_col_x + 1, ey, event_col_w - 2, eh,
                  fill_color=C_ACCENT_BG, stroke_color=C_ACCENT, radius=2)

        # Event title
        draw_text(c, event_col_x + 3 * mm, ey + eh - 4 * mm,
                  event.title, size=7.5, bold=True, color=C_ACCENT,
                  max_width=event_col_w - 8 * mm)

        # Time + duration
        if eh > 8 * mm:
            meta = f"{_time_str(event, tz)}  {event.duration_str}"
            draw_text(c, event_col_x + 3 * mm, ey + eh - 9 * mm,
                      meta, size=6, color=C_DARK_GREY,
                      max_width=event_col_w - 8 * mm)

        # Location
        if event.location and eh > 13 * mm:
            draw_text(c, event_col_x + 3 * mm, ey + eh - 13 * mm,
                      event.location, size=6, color=C_MID_GREY,
                      max_width=event_col_w - 8 * mm)

        # Tap target -> meeting note page
        if event.id in event_page_map:
            c.linkAbsolute("", f"event_{event.id}",
                           (event_col_x + 1, ey, event_col_x + event_col_w - 1, ey + eh))


# ---------------------------------------------------------------------------
# Meeting note page
# ---------------------------------------------------------------------------

def draw_meeting_page(c: canvas.Canvas, event: CalendarEvent,
                      day_page_num: int, tz: ZoneInfo = None):
    """
    Draws a meeting note page for a single event.
    Includes metadata header and a lined writing area below.
    """
    tz = tz or timezone.utc
    local_start = event.start.astimezone(tz)
    day_label = local_start.strftime("%A %-d %B")
    time_label = _time_str(event, tz)
    content_y = draw_header(c, event.title, day_label,
                            page_label=time_label)

    # Back link to daily page
    day_key = event.start.strftime("%Y-%m-%d")
    c.setFont("Helvetica", 7)
    c.setFillColor(C_ACCENT)
    back_x = PAGE_W - MARGIN
    back_y = PAGE_H - MARGIN - 6 * mm
    c.drawRightString(back_x, back_y, f"< {event.start.strftime('%A')}")
    c.linkAbsolute("", f"day_{day_key}",
                   (back_x - 20 * mm, back_y - 2 * mm, back_x, back_y + 4 * mm))

    y = content_y

    # Metadata pills row
    meta_items = [
        ("Time", time_label),
        ("Duration", event.duration_str),
    ]
    if event.location:
        meta_items.append(("Location", event.location))
    if event.calendar_name:
        meta_items.append(("Calendar", event.calendar_name))

    pill_x = MARGIN
    pill_h = 7 * mm
    pill_padding = 3 * mm

    for label, value in meta_items:
        label_w = c.stringWidth(label + ": ", "Helvetica-Bold", 7)
        value_w = c.stringWidth(value, "Helvetica", 7)
        pill_w = label_w + value_w + 2 * pill_padding

        if pill_x + pill_w > PAGE_W - MARGIN:
            pill_x = MARGIN
            y -= pill_h + 2 * mm

        draw_rect(c, pill_x, y - pill_h, pill_w, pill_h,
                  fill_color=colors.HexColor("#F5F5F5"),
                  stroke_color=C_LIGHT_GREY, radius=3)
        draw_text(c, pill_x + pill_padding, y - pill_h + 2 * mm,
                  f"{label}: ", size=7, bold=True, color=C_DARK_GREY)
        draw_text(c, pill_x + pill_padding + label_w,
                  y - pill_h + 2 * mm, value, size=7, color=C_DARK_GREY)

        pill_x += pill_w + 2 * mm

    y -= pill_h + 4 * mm

    # Attendees section
    if event.attendees:
        draw_rule(c, MARGIN, y, CONTENT_W, color=C_RULE)
        y -= 5 * mm
        draw_text(c, MARGIN, y, "Attendees", size=7.5, bold=True, color=C_DARK_GREY)
        y -= 5 * mm

        response_colors = {
            "accepted": colors.HexColor("#4CAF50"),
            "tentative": colors.HexColor("#FF9800"),
            "declined": colors.HexColor("#F44336"),
            "unknown": C_MID_GREY,
        }

        att_x = MARGIN
        for att in event.attendees:
            dot_color = response_colors.get(att.response, C_MID_GREY)
            label = att.name or att.email
            label_w = c.stringWidth(label, "Helvetica", 7.5) + 8 * mm

            if att_x + label_w > PAGE_W - MARGIN:
                att_x = MARGIN
                y -= 5 * mm

            # Response dot
            c.setFillColor(dot_color)
            c.circle(att_x + 2 * mm, y + 1.5 * mm, 1.5, fill=1, stroke=0)

            draw_text(c, att_x + 5 * mm, y, label, size=7.5, color=C_DARK_GREY)
            att_x += label_w + 4 * mm

        y -= 6 * mm

    # Description / agenda
    if event.description:
        draw_rule(c, MARGIN, y, CONTENT_W, color=C_RULE)
        y -= 5 * mm
        draw_text(c, MARGIN, y, "Agenda", size=7.5, bold=True, color=C_DARK_GREY)
        y -= 5 * mm

        # Word-wrap the description
        words = event.description.split()
        line = ""
        for word in words:
            test = (line + " " + word).strip()
            if c.stringWidth(test, "Helvetica", 7.5) < CONTENT_W:
                line = test
            else:
                if y < MARGIN + 30 * mm:
                    break
                draw_text(c, MARGIN, y, line, size=7.5, color=C_DARK_GREY)
                y -= 4.5 * mm
                line = word
        if line and y > MARGIN + 30 * mm:
            draw_text(c, MARGIN, y, line, size=7.5, color=C_DARK_GREY)
            y -= 6 * mm

    # Notes header
    draw_rule(c, MARGIN, y, CONTENT_W, color=C_RULE)
    y -= 5 * mm
    draw_text(c, MARGIN, y, "Notes", size=7.5, bold=True, color=C_DARK_GREY)
    y -= 6 * mm

    # Lined writing area
    line_spacing = 8 * mm
    while y > MARGIN + line_spacing:
        draw_rule(c, MARGIN, y, CONTENT_W, color=C_RULE, thickness=0.35)
        y -= line_spacing


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_planner(
    events: list[CalendarEvent],
    output_path: str,
    start_date: date = None,
    end_date: date = None,
    title: str = "Planner",
    timezone_name: str = "UTC",
):
    """
    Builds the full linked PDF planner.

    Pass a flat list of CalendarEvent objects (from calendar_manager).
    The function handles page ordering, internal link registration,
    and hyperlink wiring automatically.
    """
    if not start_date:
        start_date = date.today()
    if not end_date:
        end_date = start_date + timedelta(days=30)

    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = ZoneInfo("UTC")

    # Determine months to cover
    months = []
    cursor = date(start_date.year, start_date.month, 1)
    while cursor <= end_date:
        months.append((cursor.year, cursor.month))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)

    # Group events
    events_by_month: dict[tuple, list[CalendarEvent]] = {m: [] for m in months}
    events_by_day: dict[str, list[CalendarEvent]] = {}
    for e in events:
        key = (e.start.year, e.start.month)
        if key in events_by_month:
            events_by_month[key].append(e)
        day_key = e.start.strftime("%Y-%m-%d")
        events_by_day.setdefault(day_key, []).append(e)

    # Days in range that have events (or are just within range)
    all_days = []
    d = start_date
    while d <= end_date:
        all_days.append(d)
        d += timedelta(days=1)

    # --- Pass 1: assign page numbers ---
    # Page layout:
    #   Page 1 = month 1 overview
    #   Pages 2..N = daily pages for month 1
    #   Page N+1 = month 2 overview
    #   etc.
    #   Then all meeting note pages at the end

    page_num = 1
    month_page_map: dict[str, int] = {}   # "YYYY-MM" -> page number
    day_page_map: dict[str, int] = {}     # "YYYY-MM-DD" -> page number
    event_page_map: dict[str, int] = {}   # event.id -> page number

    for year, month in months:
        month_key = f"{year}-{month:02d}"
        month_page_map[month_key] = page_num
        page_num += 1

        for d in all_days:
            if d.year == year and d.month == month:
                day_key = d.strftime("%Y-%m-%d")
                day_page_map[day_key] = page_num
                page_num += 1

    # Meeting note pages (one per timed event that has a day page)
    all_timed_events = [
        e for e in events
        if not e.is_all_day and e.start.strftime("%Y-%m-%d") in day_page_map
    ]
    for e in sorted(all_timed_events, key=lambda x: x.start):
        event_page_map[e.id] = page_num
        page_num += 1

    total_pages = page_num - 1

    # --- Pass 2: draw pages ---
    c = canvas.Canvas(output_path, pagesize=A4)
    c.setTitle(title)
    c.setAuthor("PolarisFolio Clone")

    for year, month in months:
        month_key = f"{year}-{month:02d}"

        # Register bookmark for month page
        c.bookmarkPage(f"month_{year}_{month:02d}")
        c.addOutlineEntry(
            f"{datetime(year, month, 1).strftime('%B %Y')}",
            f"month_{year}_{month:02d}", level=0
        )

        # Monthly overview
        draw_month_page(c, year, month,
                        events_by_month[(year, month)],
                        day_page_map)
        c.showPage()

        # Daily pages
        for d in all_days:
            if d.year == year and d.month == month:
                day_key = d.strftime("%Y-%m-%d")
                day_events = events_by_day.get(day_key, [])

                c.bookmarkPage(f"day_{day_key}")
                c.addOutlineEntry(
                    d.strftime("%-d %B"),
                    f"day_{day_key}", level=1
                )

                draw_day_page(
                    c, d, day_events,
                    event_page_map,
                    month_page_map[month_key],
                    tz=tz,
                )
                c.showPage()

    # Meeting note pages
    for e in sorted(all_timed_events, key=lambda x: x.start):
        day_key = e.start.strftime("%Y-%m-%d")
        c.bookmarkPage(f"event_{e.id}")
        c.addOutlineEntry(
            f"  {e.title[:40]}",
            f"event_{e.id}", level=2
        )

        draw_meeting_page(c, e, day_page_map.get(day_key, 1), tz=tz)
        c.showPage()

    c.save()
    print(f"PDF saved: {output_path}  ({total_pages} pages)")
    return output_path


# ---------------------------------------------------------------------------
# Quick test with synthetic events
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import datetime, timezone
    from models import CalendarEvent, Attendee

    today = date.today()
    start = today
    end = today + timedelta(days=13)

    # Synthetic test events
    def make_event(day_offset, hour, duration_h, title, location=None,
                   description=None, attendees=None):
        d = today + timedelta(days=day_offset)
        start_dt = datetime(d.year, d.month, d.day, hour, 0, tzinfo=timezone.utc)
        end_dt = datetime(d.year, d.month, d.day,
                          hour + duration_h, 0, tzinfo=timezone.utc)
        return CalendarEvent(
            id=f"evt_{day_offset}_{hour}",
            title=title,
            start=start_dt,
            end=end_dt,
            location=location,
            description=description,
            attendees=attendees or [],
            calendar_name="Microsoft 365",
            source="graph",
        )

    test_events = [
        make_event(0, 9, 1, "Team Standup", "Teams",
                   "Daily check-in. Topics: sprint progress, blockers.",
                   [Attendee("Elliot Lawrence", "elliot@ad.com.au", "accepted"),
                    Attendee("Peter Smith", "peter@ad.com.au", "accepted")]),
        make_event(0, 10, 2, "Azure Development Group - QBR",
                   "Level 7, 123 Eagle St, Brisbane",
                   "Quarterly business review. Agenda: roadmap review, M365 licence analysis, Copilot rollout.",
                   [Attendee("Tom Deakin", "tom@azd.com.au", "accepted"),
                    Attendee("Elliot Lawrence", "elliot@ad.com.au", "accepted"),
                    Attendee("Genna Boylan", "genna@ad.com.au", "tentative")]),
        make_event(0, 14, 1, "1:1 with Peter", None,
                   "Monthly team lead catchup. Topics: utilisation, upcoming leave.",
                   [Attendee("Peter Smith", "peter@ad.com.au", "accepted")]),
        make_event(1, 9, 1, "L10 Weekly Meeting", "Boardroom",
                   "Leadership team weekly. Scorecard review, IDS.",
                   [Attendee("Aaron Lindner", "aaron@ad.com.au", "accepted"),
                    Attendee("Morgan Orreal", "morgan@ad.com.au", "accepted"),
                    Attendee("Myles Dawson", "myles@ad.com.au", "declined")]),
        make_event(2, 11, 1, "Elementa Markets - Service Review",
                   "Teams",
                   "Monthly service delivery review. Billing query follow-up.",
                   [Attendee("Elliot Lawrence", "elliot@ad.com.au", "accepted")]),
        make_event(3, 9, 2, "Superior Engineering - Onsite Visit",
                   "Superior Engineering HQ",
                   "Scheduled onsite. Windows 11 upgrade follow-up, Wasabi backup review.",
                   [Attendee("Elliot Lawrence", "elliot@ad.com.au", "accepted")]),
        make_event(5, 14, 1, "SDM Sync - Atlantic Digital",
                   "Boardroom",
                   "Internal SDM team sync. Utilisation report review.",
                   [Attendee("Elliot Lawrence", "elliot@ad.com.au", "accepted"),
                    Attendee("Peter Smith", "peter@ad.com.au", "accepted")]),
        make_event(7, 9, 1, "Team Standup", "Teams", None,
                   [Attendee("Elliot Lawrence", "elliot@ad.com.au", "accepted")]),
        make_event(7, 11, 2, "Life Fertility Clinic - IT Committee",
                   "Life Fertility Clinic North Lakes",
                   "Bi-monthly IT committee. Genie stability update, hardware refresh.",
                   [Attendee("Elliot Lawrence", "elliot@ad.com.au", "accepted")]),
        make_event(10, 9, 1, "Team Standup", "Teams", None,
                   [Attendee("Elliot Lawrence", "elliot@ad.com.au", "accepted")]),
        make_event(11, 15, 1, "1:1 with Aaron", None,
                   "Monthly check-in with service delivery lead.",
                   [Attendee("Aaron Lindner", "aaron@ad.com.au", "accepted"),
                    Attendee("Elliot Lawrence", "elliot@ad.com.au", "accepted")]),
    ]

    out = "/home/claude/polarisfolio/test_planner.pdf"
    build_planner(test_events, out, start_date=start, end_date=end,
                  title="PolarisFolio Test Planner")
