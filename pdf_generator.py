"""
PDF generator for the PolarisFolio planner — Dayfolio-style weekly spread.

Produces a hyperlinked PDF with:
  - Monthly overview page  (tap a day → weekly page)
  - Weekly spread pages    (Mon–Fri columns, timed event blocks)
  - Per-event meeting note pages (metadata + lined writing area)

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
# Page dimensions (A4 = 210 × 297 mm)
# ---------------------------------------------------------------------------
PAGE_W, PAGE_H = A4
MARGIN = 14 * mm
TAB_W  = 7 * mm          # right-edge month tab width
CONTENT_W = PAGE_W - 2 * MARGIN - TAB_W

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------
C_BLACK      = colors.HexColor("#1A1A1A")
C_DARK_GREY  = colors.HexColor("#444444")
C_MID_GREY   = colors.HexColor("#888888")
C_LIGHT_GREY = colors.HexColor("#CCCCCC")
C_RULE       = colors.HexColor("#E8E8E8")
C_ACCENT     = colors.HexColor("#5C6BC0")
C_ACCENT_BG  = colors.HexColor("#EEF0FB")
C_WHITE      = colors.white

# Pastel event block colours (Dayfolio-inspired) — solid fill, white text
EVENT_COLORS = [
    colors.HexColor("#7ECFCF"),   # teal
    colors.HexColor("#C8A8E8"),   # lavender
    colors.HexColor("#F4A8C0"),   # rose
    colors.HexColor("#F9DC90"),   # yellow
    colors.HexColor("#90C4F4"),   # sky blue
    colors.HexColor("#A8E8B4"),   # mint
    colors.HexColor("#F4C090"),   # peach
]

# Right-edge tab colour per month (Jan=0 … Dec=11)
MONTH_TAB_COLORS = [
    "#A8D8EA", "#B0E0D8", "#C8D0F0", "#D8C0E8",   # Jan–Apr
    "#E8C0D0", "#F4D4A0", "#F4E8A0", "#D8F0C0",   # May–Aug
    "#C0E8D0", "#A8C8E8", "#C0A8D8", "#E8B8C0",   # Sep–Dec
]

HOUR_START = 7
HOUR_END   = 19


def _event_color(title: str) -> colors.Color:
    """Consistent pastel colour based on event title hash."""
    return EVENT_COLORS[hash(title) % len(EVENT_COLORS)]


def _week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def draw_text(c: canvas.Canvas, x: float, y: float, text: str,
              size: float = 9, bold: bool = False,
              color=None, align: str = "left", max_width: float = None):
    font = "Helvetica-Bold" if bold else "Helvetica"
    c.setFont(font, size)
    c.setFillColor(color or C_BLACK)
    if max_width:
        while text and c.stringWidth(text, font, size) > max_width:
            text = text[:-1]
        if len(text) < len(text):      # was truncated
            text = text[:-1] + "…"
    if align == "right":
        c.drawRightString(x, y, text)
    elif align == "center":
        c.drawCentredString(x, y, text)
    else:
        c.drawString(x, y, text)


def draw_rule(c: canvas.Canvas, x, y, width, color=None, thickness=0.4):
    c.setStrokeColor(color or C_RULE)
    c.setLineWidth(thickness)
    c.line(x, y, x + width, y)


def draw_rule_v(c: canvas.Canvas, x, y_bottom, height, color=None, thickness=0.3):
    c.setStrokeColor(color or C_RULE)
    c.setLineWidth(thickness)
    c.line(x, y_bottom, x, y_bottom + height)


def draw_rect(c: canvas.Canvas, x, y, w, h,
              fill_color=None, stroke_color=None, radius=2):
    c.setFillColor(fill_color or C_WHITE)
    c.setStrokeColor(stroke_color or C_LIGHT_GREY)
    c.setLineWidth(0.5)
    c.roundRect(x, y, w, h, radius,
                fill=1 if fill_color else 0,
                stroke=1 if stroke_color else 0)


def _time_str(event, tz) -> str:
    if event.is_all_day:
        return "All day"
    s = event.start.astimezone(tz)
    e = event.end.astimezone(tz)
    return f"{s.strftime('%I:%M %p')} – {e.strftime('%I:%M %p')}"


# ---------------------------------------------------------------------------
# Right-edge month tab
# ---------------------------------------------------------------------------

def draw_month_tab(c: canvas.Canvas, month: int, label: str = None):
    """Draws the coloured right-edge tab for the given month (1-based)."""
    col = colors.HexColor(MONTH_TAB_COLORS[(month - 1) % 12])
    tab_x = PAGE_W - TAB_W
    c.setFillColor(col)
    c.rect(tab_x, 0, TAB_W, PAGE_H, fill=1, stroke=0)

    # Month abbreviation rotated 90° (reads bottom → top)
    abbr = (label or datetime(2026, month, 1).strftime("%b")).upper()
    c.saveState()
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 6)
    c.translate(tab_x + TAB_W / 2, PAGE_H / 2)
    c.rotate(90)
    c.drawCentredString(0, -2, abbr)
    c.restoreState()


# ---------------------------------------------------------------------------
# Monthly overview page
# ---------------------------------------------------------------------------

def draw_month_page(c: canvas.Canvas, year: int, month: int,
                    events: list[CalendarEvent],
                    day_week_map: dict[str, str]):
    """
    Monthly calendar grid. Each day cell links to its weekly page.
    day_week_map: "YYYY-MM-DD" -> bookmark key of the containing week page.
    """
    draw_month_tab(c, month)

    month_name = datetime(year, month, 1).strftime("%B").upper()
    year_str   = str(year)

    # Header
    header_y = PAGE_H - MARGIN - 6 * mm
    c.setFont("Helvetica-Bold", 15)
    c.setFillColor(C_BLACK)
    c.drawString(MARGIN, header_y, month_name)
    mw = c.stringWidth(month_name, "Helvetica-Bold", 15)
    c.setFont("Helvetica", 15)
    c.setFillColor(C_LIGHT_GREY)
    c.drawString(MARGIN + mw + 3 * mm, header_y, year_str)

    draw_rule(c, MARGIN, header_y - 9 * mm, CONTENT_W, color=C_LIGHT_GREY, thickness=0.5)

    # Group events by day
    events_by_day: dict[int, list] = {}
    for e in events:
        if e.start.month == month and e.start.year == year:
            events_by_day.setdefault(e.start.day, []).append(e)

    cal = calendar.monthcalendar(year, month)
    num_weeks = len(cal)

    grid_top = header_y - 14 * mm
    grid_h   = grid_top - MARGIN - 4 * mm
    cell_w   = CONTENT_W / 7
    cell_h   = grid_h / num_weeks

    # Day-of-week headers (Mon … Sun)
    dow_labels = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    for col, label in enumerate(dow_labels):
        x = MARGIN + col * cell_w
        is_weekend = col >= 5
        draw_text(c, x + cell_w / 2, grid_top - 4 * mm, label,
                  size=6.5, bold=True,
                  color=C_MID_GREY if is_weekend else C_DARK_GREY,
                  align="center")

    cell_top = grid_top - 7 * mm
    today = date.today()

    for row, week in enumerate(cal):
        for col, day_num in enumerate(week):
            if day_num == 0:
                continue

            x = MARGIN + col * cell_w
            y = cell_top - (row + 1) * cell_h
            is_today   = (date(year, month, day_num) == today)
            is_weekend = col >= 5
            day_key = f"{year}-{month:02d}-{day_num:02d}"

            # Today highlight
            if is_today:
                draw_rect(c, x + 1, y + 1, cell_w - 2, cell_h - 2,
                          fill_color=C_ACCENT_BG, stroke_color=C_ACCENT, radius=3)

            # Day number
            num_color = C_ACCENT if is_today else (C_MID_GREY if is_weekend else C_BLACK)
            draw_text(c, x + 3 * mm, y + cell_h - 5 * mm, str(day_num),
                      size=9, bold=is_today, color=num_color)

            # Events in cell
            if day_num in events_by_day:
                day_evts = events_by_day[day_num]
                # First event name
                draw_text(c, x + 2 * mm, y + cell_h - 9 * mm,
                          day_evts[0].title, size=5, color=C_DARK_GREY,
                          max_width=cell_w - 4 * mm)
                # Dot if more
                if len(day_evts) > 1:
                    c.setFillColor(C_ACCENT)
                    c.circle(x + cell_w - 4 * mm, y + cell_h - 4 * mm,
                             1.5, fill=1, stroke=0)

            # Tap target → weekly page
            if day_key in day_week_map:
                c.linkAbsolute("", day_week_map[day_key],
                               (x + 1, y + 1, x + cell_w - 1, y + cell_h - 1))

            # Cell border
            c.setStrokeColor(C_RULE)
            c.setLineWidth(0.3)
            c.rect(x, y, cell_w, cell_h, fill=0, stroke=1)


# ---------------------------------------------------------------------------
# Weekly spread page
# ---------------------------------------------------------------------------

def draw_week_page(c: canvas.Canvas,
                   week_monday: date,
                   days_events: dict[str, list[CalendarEvent]],
                   event_page_map: dict[str, int],
                   tz: ZoneInfo):
    """
    5-column weekly spread (Mon–Fri) with timed event blocks.
    Dayfolio-inspired: large day numbers, pastel event blocks, bottom sections.
    """
    week_friday = week_monday + timedelta(days=4)
    month = week_monday.month
    week_num = week_monday.isocalendar()[1]

    draw_month_tab(c, month)

    # ---------- Header ----------
    header_y = PAGE_H - MARGIN - 6 * mm
    month_name = week_monday.strftime("%B").upper()
    year_str   = str(week_monday.year)

    c.setFont("Helvetica-Bold", 14)
    c.setFillColor(C_BLACK)
    c.drawString(MARGIN, header_y, month_name)
    mw = c.stringWidth(month_name, "Helvetica-Bold", 14)

    c.setFont("Helvetica", 14)
    c.setFillColor(C_LIGHT_GREY)
    c.drawString(MARGIN + mw + 3 * mm, header_y, year_str)

    # Week number (top right, inside content area)
    draw_text(c, MARGIN + CONTENT_W, header_y, str(week_num),
              size=9, color=C_MID_GREY, align="right")

    # "WEEKLY PLAN" centred
    c.setFont("Helvetica", 6.5)
    c.setFillColor(C_LIGHT_GREY)
    c.drawCentredString(MARGIN + CONTENT_W / 2, header_y, "WEEKLY PLAN")

    # Week date range
    if week_monday.month == week_friday.month:
        date_range = f"WEEK {week_num}  ·  {week_monday.strftime('%-d')} – {week_friday.strftime('%-d %b').upper()}"
    else:
        date_range = f"WEEK {week_num}  ·  {week_monday.strftime('%-d %b').upper()} – {week_friday.strftime('%-d %b').upper()}"

    draw_text(c, MARGIN, header_y - 6 * mm, date_range,
              size=7, bold=True, color=C_DARK_GREY)

    sep_y = header_y - 10 * mm
    draw_rule(c, MARGIN, sep_y, CONTENT_W, color=C_LIGHT_GREY, thickness=0.5)

    # ---------- Grid layout ----------
    time_col_w = 9 * mm
    day_col_w  = (CONTENT_W - time_col_w) / 5

    bottom_h   = 38 * mm
    grid_top   = sep_y - 12 * mm   # leave room for day number headers
    day_hdr_y  = sep_y - 2 * mm    # y for day numbers (just below sep line)
    grid_bottom = MARGIN + bottom_h
    grid_h     = grid_top - grid_bottom

    num_hours  = HOUR_END - HOUR_START
    slot_h     = grid_h / num_hours

    # ---------- Day column headers ----------
    days = [week_monday + timedelta(days=i) for i in range(5)]
    today = date.today()

    for i, day in enumerate(days):
        col_x = MARGIN + time_col_w + i * day_col_w
        is_today = (day == today)

        # Day number
        num_color = C_ACCENT if is_today else C_BLACK
        day_num_str = str(day.day)
        c.setFont("Helvetica-Bold" if is_today else "Helvetica", 13)
        c.setFillColor(num_color)
        c.drawString(col_x + 2 * mm, day_hdr_y - 4 * mm, day_num_str)

        # Day abbreviation (small, grey)
        draw_text(c, col_x + 2 * mm, day_hdr_y - 9 * mm,
                  day.strftime("%a").upper(), size=5.5, color=C_MID_GREY)

        # Vertical column separator
        if i > 0:
            draw_rule_v(c, col_x, grid_bottom, grid_h + 12 * mm,
                        color=C_RULE, thickness=0.3)

    # ---------- Horizontal time rules ----------
    for i in range(num_hours + 1):
        hour = HOUR_START + i
        y = grid_top - i * slot_h

        # Hour label (left of grid)
        if i < num_hours:
            label = f"{hour:02d}"
            draw_text(c, MARGIN, y - 1 * mm, label, size=5.5, color=C_LIGHT_GREY)

        # Rule across all day columns
        draw_rule(c, MARGIN + time_col_w, y, CONTENT_W - time_col_w,
                  color=C_RULE, thickness=0.35)

        # 30-min half-hour rule (lighter)
        if i < num_hours:
            half_y = y - slot_h / 2
            draw_rule(c, MARGIN + time_col_w, half_y, CONTENT_W - time_col_w,
                      color=colors.HexColor("#F2F2F2"), thickness=0.2)

    # ---------- Event blocks ----------
    for day_idx, day in enumerate(days):
        day_key = day.strftime("%Y-%m-%d")
        day_evts = sorted(days_events.get(day_key, []), key=lambda e: e.start)
        col_x = MARGIN + time_col_w + day_idx * day_col_w

        # All-day strip (thin bar under day number)
        all_day = [e for e in day_evts if e.is_all_day]
        for ad_i, ad_evt in enumerate(all_day[:2]):
            bar_y = day_hdr_y - 10 * mm - ad_i * 4 * mm
            c.setFillColor(_event_color(ad_evt.title))
            c.roundRect(col_x + 1, bar_y, day_col_w - 2, 3.5 * mm, 1, fill=1, stroke=0)
            draw_text(c, col_x + 2 * mm, bar_y + 0.8 * mm,
                      ad_evt.title, size=5, color=colors.white,
                      max_width=day_col_w - 4 * mm)

        # Timed events — detect overlaps and split width
        timed = [e for e in day_evts if not e.is_all_day]

        # Simple overlap detection: group events into non-overlapping lanes
        lanes: list[list] = []
        for evt in timed:
            placed = False
            for lane in lanes:
                last = lane[-1]
                last_end = last.end.astimezone(tz)
                evt_start = evt.start.astimezone(tz)
                if evt_start >= last_end:
                    lane.append(evt)
                    placed = True
                    break
            if not placed:
                lanes.append([evt])

        num_lanes = len(lanes)

        for lane_idx, lane in enumerate(lanes):
            lane_x = col_x + lane_idx * (day_col_w / max(num_lanes, 1))
            lane_w = day_col_w / max(num_lanes, 1)

            for evt in lane:
                local_start = evt.start.astimezone(tz)
                local_end   = evt.end.astimezone(tz)

                if local_start.hour >= HOUR_END or local_end.hour < HOUR_START:
                    continue

                start_frac = max(local_start.hour + local_start.minute / 60, HOUR_START) - HOUR_START
                end_frac   = min(local_end.hour   + local_end.minute   / 60, HOUR_END)   - HOUR_START
                block_h    = (end_frac - start_frac) * slot_h - 1
                block_top  = grid_top - start_frac * slot_h
                block_bot  = block_top - block_h

                evt_col = _event_color(evt.title)

                # Block fill (rounded)
                c.setFillColor(evt_col)
                c.setLineWidth(0)
                c.roundRect(lane_x + 0.5, block_bot, lane_w - 1, block_h, 2,
                            fill=1, stroke=0)

                # Title (white)
                if block_h > 4 * mm:
                    draw_text(c, lane_x + 2 * mm, block_top - 4 * mm,
                              evt.title, size=5.5, bold=True, color=colors.white,
                              max_width=lane_w - 4 * mm)

                # Time (white, smaller)
                if block_h > 9 * mm:
                    t_str = f"{local_start.strftime('%H:%M')}–{local_end.strftime('%H:%M')}"
                    draw_text(c, lane_x + 2 * mm, block_top - 8.5 * mm,
                              t_str, size=4.5, color=colors.white,
                              max_width=lane_w - 4 * mm)

                # Tap target → meeting note page
                if evt.id in event_page_map:
                    c.linkAbsolute("", f"event_{evt.id}",
                                   (lane_x + 0.5, block_bot, lane_x + lane_w - 0.5, block_top))

    # ---------- Bottom sections: FOCUS | PRIORITIES | NOTES ----------
    sect_top = grid_bottom - 1 * mm
    sect_w   = CONTENT_W / 3

    for idx, (label, numbered) in enumerate([("FOCUS", False),
                                              ("PRIORITIES", True),
                                              ("NOTES", False)]):
        sx = MARGIN + idx * sect_w

        # Section heading
        draw_text(c, sx, sect_top, label, size=5.5, bold=True, color=C_MID_GREY)
        draw_rule(c, sx, sect_top - 2 * mm, sect_w - 2 * mm, color=C_RULE, thickness=0.5)

        # Lines / numbered lines
        ly = sect_top - 7 * mm
        line_num = 1
        while ly > MARGIN + 1 * mm:
            if numbered:
                draw_text(c, sx, ly, str(line_num), size=6, color=C_LIGHT_GREY)
                draw_rule(c, sx + 4 * mm, ly - 1 * mm, sect_w - 6 * mm,
                          color=C_RULE, thickness=0.3)
                line_num += 1
            else:
                draw_rule(c, sx, ly - 1 * mm, sect_w - 2 * mm,
                          color=C_RULE, thickness=0.3)
            ly -= 6 * mm


# ---------------------------------------------------------------------------
# Meeting note page
# ---------------------------------------------------------------------------

def draw_meeting_page(c: canvas.Canvas, event: CalendarEvent,
                      week_bookmark: str, tz: ZoneInfo = None):
    """Meeting notes: metadata header + lined writing area."""
    tz = tz or timezone.utc
    local_start = event.start.astimezone(tz)
    month = local_start.month

    draw_month_tab(c, month)

    title = event.title
    day_label  = local_start.strftime("%A %-d %B")
    time_label = _time_str(event, tz)

    # Header
    header_y = PAGE_H - MARGIN - 6 * mm
    draw_text(c, MARGIN, header_y, title, size=13, bold=True,
              max_width=CONTENT_W - 20 * mm)
    draw_text(c, MARGIN, header_y - 6 * mm, day_label, size=8, color=C_MID_GREY)
    draw_text(c, MARGIN + CONTENT_W, header_y, time_label,
              size=7.5, color=C_MID_GREY, align="right")

    # Back link → weekly page
    c.setFont("Helvetica", 7)
    c.setFillColor(C_ACCENT)
    back_x = MARGIN + CONTENT_W
    back_y = header_y - 6 * mm
    c.drawRightString(back_x, back_y, f"< Week")
    c.linkAbsolute("", week_bookmark,
                   (back_x - 15 * mm, back_y - 2 * mm, back_x, back_y + 4 * mm))

    draw_rule(c, MARGIN, header_y - 11 * mm, CONTENT_W, color=C_LIGHT_GREY, thickness=0.5)

    y = header_y - 17 * mm

    # Metadata pills
    meta = [("Time", time_label), ("Duration", event.duration_str)]
    if event.location:     meta.append(("Location", event.location))
    if event.calendar_name: meta.append(("Calendar", event.calendar_name))

    pill_x = MARGIN
    pill_h = 6.5 * mm
    for lbl, val in meta:
        lw = c.stringWidth(lbl + ": ", "Helvetica-Bold", 7)
        vw = c.stringWidth(val, "Helvetica", 7)
        pw = lw + vw + 6 * mm
        if pill_x + pw > MARGIN + CONTENT_W:
            pill_x = MARGIN
            y -= pill_h + 2 * mm
        draw_rect(c, pill_x, y - pill_h, pw, pill_h,
                  fill_color=colors.HexColor("#F5F5F5"),
                  stroke_color=C_LIGHT_GREY, radius=3)
        draw_text(c, pill_x + 3 * mm, y - pill_h + 1.5 * mm,
                  f"{lbl}: ", size=7, bold=True, color=C_DARK_GREY)
        draw_text(c, pill_x + 3 * mm + lw, y - pill_h + 1.5 * mm,
                  val, size=7, color=C_DARK_GREY)
        pill_x += pw + 2 * mm

    y -= pill_h + 5 * mm

    # Attendees
    if event.attendees:
        draw_rule(c, MARGIN, y, CONTENT_W, color=C_RULE)
        y -= 5 * mm
        draw_text(c, MARGIN, y, "Attendees", size=7.5, bold=True, color=C_DARK_GREY)
        y -= 5 * mm
        resp_colors = {"accepted": colors.HexColor("#4CAF50"),
                       "tentative": colors.HexColor("#FF9800"),
                       "declined":  colors.HexColor("#F44336"),
                       "unknown":   C_MID_GREY}
        att_x = MARGIN
        for att in event.attendees:
            lbl = att.name or att.email
            lw  = c.stringWidth(lbl, "Helvetica", 7.5) + 8 * mm
            if att_x + lw > MARGIN + CONTENT_W:
                att_x = MARGIN; y -= 5 * mm
            c.setFillColor(resp_colors.get(att.response, C_MID_GREY))
            c.circle(att_x + 2 * mm, y + 1.5 * mm, 1.5, fill=1, stroke=0)
            draw_text(c, att_x + 5 * mm, y, lbl, size=7.5, color=C_DARK_GREY)
            att_x += lw + 4 * mm
        y -= 6 * mm

    # Agenda / description
    if event.description:
        draw_rule(c, MARGIN, y, CONTENT_W, color=C_RULE)
        y -= 5 * mm
        draw_text(c, MARGIN, y, "Agenda", size=7.5, bold=True, color=C_DARK_GREY)
        y -= 5 * mm
        words = event.description.split()
        line  = ""
        for word in words:
            test = (line + " " + word).strip()
            if c.stringWidth(test, "Helvetica", 7.5) < CONTENT_W:
                line = test
            else:
                if y < MARGIN + 30 * mm: break
                draw_text(c, MARGIN, y, line, size=7.5, color=C_DARK_GREY)
                y -= 4.5 * mm; line = word
        if line and y > MARGIN + 30 * mm:
            draw_text(c, MARGIN, y, line, size=7.5, color=C_DARK_GREY)
            y -= 6 * mm

    # Notes section
    draw_rule(c, MARGIN, y, CONTENT_W, color=C_RULE)
    y -= 5 * mm
    draw_text(c, MARGIN, y, "Notes", size=7.5, bold=True, color=C_DARK_GREY)
    y -= 7 * mm

    # Lined writing area
    while y > MARGIN + 8 * mm:
        draw_rule(c, MARGIN, y, CONTENT_W, color=C_RULE, thickness=0.35)
        y -= 8 * mm


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

    Structure:
      - One monthly overview page per month in the range
      - One weekly spread page per ISO week in the range (Mon–Fri columns)
      - One meeting note page per timed event
    """
    if not start_date:
        start_date = date.today()
    if not end_date:
        end_date = start_date + timedelta(days=30)

    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = ZoneInfo("UTC")

    # Months to cover
    months = []
    cursor = date(start_date.year, start_date.month, 1)
    while cursor <= end_date:
        months.append((cursor.year, cursor.month))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)

    # Weeks to cover (each week starts on Monday)
    first_monday = _week_monday(start_date)
    last_monday  = _week_monday(end_date)
    weeks = []
    w = first_monday
    while w <= last_monday:
        weeks.append(w)
        w += timedelta(days=7)

    # Group events
    events_by_month: dict[tuple, list] = {m: [] for m in months}
    events_by_day:   dict[str, list]   = {}
    for e in events:
        key = (e.start.year, e.start.month)
        if key in events_by_month:
            events_by_month[key].append(e)
        day_key = e.start.astimezone(tz).strftime("%Y-%m-%d")
        events_by_day.setdefault(day_key, []).append(e)

    # Mapping: day -> which week bookmark it belongs to
    day_week_map: dict[str, str] = {}   # "YYYY-MM-DD" -> week bookmark key
    for w in weeks:
        bm = f"week_{w.isoformat()}"
        for i in range(5):
            dk = (w + timedelta(days=i)).strftime("%Y-%m-%d")
            day_week_map[dk] = bm

    # --- Pass 1: assign page numbers ---
    page_num = 1
    month_page_map: dict[str, int] = {}   # "YYYY-MM" -> page num
    week_page_map:  dict[str, int] = {}   # monday iso -> page num

    for year, month in months:
        month_page_map[f"{year}-{month:02d}"] = page_num
        page_num += 1

    for w in weeks:
        week_page_map[w.isoformat()] = page_num
        page_num += 1

    # Meeting note pages — one per timed event with a known week
    all_timed = [
        e for e in events
        if not e.is_all_day
        and e.start.astimezone(tz).strftime("%Y-%m-%d") in day_week_map
    ]
    event_page_map: dict[str, int] = {}
    event_week_map: dict[str, str] = {}   # event.id -> week bookmark key
    for e in sorted(all_timed, key=lambda x: x.start):
        event_page_map[e.id] = page_num
        dk = e.start.astimezone(tz).strftime("%Y-%m-%d")
        event_week_map[e.id] = day_week_map[dk]
        page_num += 1

    total_pages = page_num - 1

    # --- Pass 2: draw pages ---
    c = canvas.Canvas(output_path, pagesize=A4)
    c.setTitle(title)
    c.setAuthor("PolarisFolio")

    # Monthly overview pages
    for year, month in months:
        month_key = f"{year}-{month:02d}"
        c.bookmarkPage(f"month_{year}_{month:02d}")
        c.addOutlineEntry(
            datetime(year, month, 1).strftime("%B %Y"),
            f"month_{year}_{month:02d}", level=0
        )
        draw_month_page(c, year, month, events_by_month[(year, month)], day_week_map)
        c.showPage()

    # Weekly spread pages
    for w in weeks:
        bm = f"week_{w.isoformat()}"
        c.bookmarkPage(bm)
        label = f"Week {w.isocalendar()[1]} · {w.strftime('%-d %b')} – {(w+timedelta(days=4)).strftime('%-d %b')}"
        c.addOutlineEntry(label, bm, level=1)

        # Collect events for this week's 5 days
        week_days_events = {}
        for i in range(5):
            dk = (w + timedelta(days=i)).strftime("%Y-%m-%d")
            week_days_events[dk] = events_by_day.get(dk, [])

        draw_week_page(c, w, week_days_events, event_page_map, tz)
        c.showPage()

    # Meeting note pages
    for e in sorted(all_timed, key=lambda x: x.start):
        week_bm = event_week_map[e.id]
        c.bookmarkPage(f"event_{e.id}")
        c.addOutlineEntry(f"  {e.title[:40]}", f"event_{e.id}", level=2)
        draw_meeting_page(c, e, week_bm, tz=tz)
        c.showPage()

    c.save()
    print(f"PDF saved: {output_path}  ({total_pages} pages)")
    return output_path


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import datetime, timezone
    from models import CalendarEvent, Attendee

    today = date.today()
    start = today
    end   = today + timedelta(days=13)

    def make_event(day_offset, hour, dur, title, loc=None, desc=None, atts=None):
        d  = today + timedelta(days=day_offset)
        s  = datetime(d.year, d.month, d.day, hour,     0, tzinfo=timezone.utc)
        en = datetime(d.year, d.month, d.day, hour+dur, 0, tzinfo=timezone.utc)
        return CalendarEvent(id=f"evt_{day_offset}_{hour}", title=title,
                             start=s, end=en, location=loc, description=desc,
                             attendees=atts or [], calendar_name="AD Calendar", source="ical")

    test_events = [
        make_event(0, 9, 1, "Team Standup", "Teams",
                   atts=[Attendee("Elliot Lawrence", "e@ad.com.au", "accepted")]),
        make_event(0, 10, 2, "Azure QBR", "Level 7, 123 Eagle St",
                   atts=[Attendee("Tom Deakin", "tom@azd.com.au", "accepted"),
                         Attendee("Genna Boylan", "genna@ad.com.au", "tentative")]),
        make_event(0, 14, 1, "1:1 with Peter",
                   atts=[Attendee("Peter Smith", "peter@ad.com.au", "accepted")]),
        make_event(1, 9,  1, "L10 Weekly", "Boardroom"),
        make_event(2, 11, 1, "Elementa Markets Review", "Teams"),
        make_event(3, 9,  2, "Superior Engineering Onsite", "Superior HQ"),
        make_event(4, 9,  1, "Team Standup", "Teams"),
        make_event(4, 11, 2, "Life Fertility IT Committee", "North Lakes"),
        make_event(7, 9,  1, "Team Standup", "Teams"),
        make_event(7, 15, 1, "SDM Sync", "Boardroom"),
        make_event(10, 9, 1, "Team Standup", "Teams"),
        make_event(11, 15,1, "1:1 with Aaron"),
    ]

    out = os.path.expanduser("~/polarisfolio_pdfs/test_weekly.pdf")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    build_planner(test_events, out, start_date=start, end_date=end,
                  title="PolarisFolio Weekly Test", timezone_name="Australia/Brisbane")
