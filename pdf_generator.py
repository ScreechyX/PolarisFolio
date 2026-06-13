"""
PDF generator for the PolarisFolio planner — Dayfolio-style weekly spread.

Structure per generated PDF:
  • Monthly overview pages  (week-number grid, event bars, tap → weekly page)
  • Weekly spread pages     (Mon–Fri columns, timed event blocks, bottom sections)
  • Meeting note pages      (metadata + ruled writing area)

Optimised for reMarkable Paper Pro (A4 portrait, colour e-ink display).
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

# ─────────────────────────────────────────────────────────────────────────────
# Page geometry
# ─────────────────────────────────────────────────────────────────────────────
PAGE_W, PAGE_H = A4          # 595.28 × 841.89 pt  (210 × 297 mm)
MARGIN    = 14 * mm
TAB_W     = 8 * mm           # right-edge month tab
CONTENT_W = PAGE_W - 2 * MARGIN - TAB_W

HOUR_START = 7
HOUR_END   = 19

# ─────────────────────────────────────────────────────────────────────────────
# Colour system
# ─────────────────────────────────────────────────────────────────────────────

# Text
C_INK       = colors.HexColor("#1C1C1E")   # near-black body text
C_INK_2     = colors.HexColor("#3C3C43")   # secondary labels
C_GREY      = colors.HexColor("#8A8A8E")   # tertiary / metadata
C_SILVER    = colors.HexColor("#C7C7CC")   # rules, faint labels
C_GHOST     = colors.HexColor("#EBEBF0")   # lightest rule / cell border
C_WHITE     = colors.white

# Accent (indigo)
C_ACCENT    = colors.HexColor("#5C6BC0")   # today circle, links, headers
C_ACCENT_LT = colors.HexColor("#EEF0FB")   # today cell wash, chip bg

# Surface tints
C_WKND      = colors.HexColor("#F7F7F9")   # weekend column wash

# Pastel event fills — white text legible on all of these
EVENT_PALETTE = [
    colors.HexColor("#5BBFBF"),  # teal
    colors.HexColor("#9B78D4"),  # violet
    colors.HexColor("#D96080"),  # rose
    colors.HexColor("#D99030"),  # amber
    colors.HexColor("#4888D4"),  # cobalt
    colors.HexColor("#48A870"),  # jade
    colors.HexColor("#D96040"),  # terra
]

# Quarter-based tab palette (Dayfolio inspiration)
#   Q1 Jan–Mar : teal        Q2 Apr–Jun : indigo
#   Q3 Jul–Sep : rose        Q4 Oct–Dec : amber
_Q = ["#5BBFBF", "#5BBFBF", "#5BBFBF",   # Q1
      "#7986CB", "#7986CB", "#7986CB",   # Q2
      "#E57080", "#E57080", "#E57080",   # Q3
      "#E5A030", "#E5A030", "#E5A030"]   # Q4
MONTH_TAB_COLORS = _Q   # index 0=Jan … 11=Dec


def _event_color(title: str) -> colors.Color:
    """Deterministic pastel colour from event title."""
    return EVENT_PALETTE[hash(title) % len(EVENT_PALETTE)]


def _week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


# ─────────────────────────────────────────────────────────────────────────────
# Low-level drawing primitives
# ─────────────────────────────────────────────────────────────────────────────

def _font(bold: bool = False, italic: bool = False) -> str:
    if bold and italic: return "Helvetica-BoldOblique"
    if bold:            return "Helvetica-Bold"
    if italic:          return "Helvetica-Oblique"
    return "Helvetica"


def _truncate(c: canvas.Canvas, text: str, font: str, size: float, max_w: float) -> str:
    if c.stringWidth(text, font, size) <= max_w:
        return text
    ellipsis = "…"
    while text and c.stringWidth(text + ellipsis, font, size) > max_w:
        text = text[:-1]
    return text + ellipsis


def txt(c: canvas.Canvas, x: float, y: float, text: str,
        size: float = 9, bold: bool = False, italic: bool = False,
        col=None, align: str = "left", max_w: float = None):
    f = _font(bold, italic)
    c.setFont(f, size)
    c.setFillColor(col if col is not None else C_INK)
    if max_w:
        text = _truncate(c, text, f, size, max_w)
    if align == "right":
        c.drawRightString(x, y, text)
    elif align == "center":
        c.drawCentredString(x, y, text)
    else:
        c.drawString(x, y, text)


def hrule(c: canvas.Canvas, x: float, y: float, w: float,
          col=None, lw: float = 0.4):
    c.setStrokeColor(col if col is not None else C_GHOST)
    c.setLineWidth(lw)
    c.line(x, y, x + w, y)


def vrule(c: canvas.Canvas, x: float, y_bot: float, h: float,
          col=None, lw: float = 0.3):
    c.setStrokeColor(col if col is not None else C_GHOST)
    c.setLineWidth(lw)
    c.line(x, y_bot, x, y_bot + h)


def filled_rect(c: canvas.Canvas, x, y, w, h, fill,
                stroke=None, lw: float = 0.5, r: float = 0):
    c.setFillColor(fill)
    if stroke:
        c.setStrokeColor(stroke)
        c.setLineWidth(lw)
    if r:
        c.roundRect(x, y, w, h, r, fill=1, stroke=1 if stroke else 0)
    else:
        c.rect(x, y, w, h, fill=1, stroke=1 if stroke else 0)


def circle(c: canvas.Canvas, cx, cy, r, fill, stroke=None, lw=0.5):
    c.setFillColor(fill)
    if stroke:
        c.setStrokeColor(stroke)
        c.setLineWidth(lw)
    c.circle(cx, cy, r, fill=1, stroke=1 if stroke else 0)


# ─────────────────────────────────────────────────────────────────────────────
# Right-edge month tab
# ─────────────────────────────────────────────────────────────────────────────

def draw_tab(c: canvas.Canvas, month: int):
    """Coloured right-edge tab with rotated month abbreviation."""
    fill = colors.HexColor(MONTH_TAB_COLORS[(month - 1) % 12])
    tab_x = PAGE_W - TAB_W

    # Tab background (full page height)
    filled_rect(c, tab_x, 0, TAB_W, PAGE_H, fill=fill)

    # Thin separator on inner edge
    vrule(c, tab_x, 0, PAGE_H, col=colors.HexColor("#00000018"), lw=0.5)

    # Month abbreviation, rotated 90° — reads bottom→top
    abbr = datetime(2026, month, 1).strftime("%b").upper()
    c.saveState()
    c.setFillColor(C_WHITE)
    c.setFont("Helvetica-Bold", 7)
    c.translate(tab_x + TAB_W / 2, PAGE_H / 2)
    c.rotate(90)
    c.drawCentredString(0, -2.5, abbr)
    c.restoreState()


# ─────────────────────────────────────────────────────────────────────────────
# Page header (shared across all page types)
# ─────────────────────────────────────────────────────────────────────────────

def draw_page_header(c: canvas.Canvas,
                     left_label: str, left_sub: str = "",
                     right_label: str = "", right_sub: str = "",
                     accent_bar: bool = True) -> float:
    """
    Draws a consistent top header and returns the y coordinate of the
    separator rule (so callers know where the body starts).

    Layout:
      ┃  LEFT_LABEL (bold, large)        right_label (small, grey)
      ┃  left_sub (small, grey)          right_sub (small, grey)
      ─────────────────────────────────────────────────────────────
    The ┃ is a short left accent bar in the accent colour.
    """
    top = PAGE_H - MARGIN

    if accent_bar:
        # 3 pt wide, 18 pt tall left accent stripe
        bar_h = 17
        filled_rect(c, MARGIN, top - bar_h + 2, 3, bar_h, fill=C_ACCENT)
        tx = MARGIN + 6
    else:
        tx = MARGIN

    # Primary label (e.g. "JUNE 2026" or meeting title)
    c.setFont("Helvetica-Bold", 15)
    c.setFillColor(C_INK)
    c.drawString(tx, top - 11, left_label)

    if left_sub:
        txt(c, tx, top - 20, left_sub, size=7, col=C_GREY)

    if right_label:
        txt(c, MARGIN + CONTENT_W, top - 11, right_label,
            size=8, col=C_GREY, align="right")
    if right_sub:
        txt(c, MARGIN + CONTENT_W, top - 20, right_sub,
            size=7, col=C_SILVER, align="right")

    sep_y = top - 26
    hrule(c, MARGIN, sep_y, CONTENT_W, col=C_SILVER, lw=0.6)
    return sep_y


# ─────────────────────────────────────────────────────────────────────────────
# Monthly overview page
# ─────────────────────────────────────────────────────────────────────────────

_DOW = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

def draw_month_page(c: canvas.Canvas, year: int, month: int,
                    events: list,
                    day_week_map: dict):
    """
    Full-page monthly calendar grid.

    Columns: week-num gutter + MON … FRI + │ SAT + SUN
    Each cell: day number (circle if today) + up to 2 event pill bars
    """
    draw_tab(c, month)
    month_name = datetime(year, month, 1).strftime("%B").upper()
    week_label = "MONTHLY OVERVIEW"
    sep_y = draw_page_header(
        c,
        left_label=f"{month_name} {year}",
        left_sub=week_label,
        accent_bar=True,
    )

    # Group events by day number
    ev_by_day: dict[int, list] = {}
    for e in events:
        loc = e.start.astimezone(ZoneInfo("UTC"))
        if loc.year == year and loc.month == month:
            ev_by_day.setdefault(loc.day, []).append(e)

    cal_weeks = calendar.monthcalendar(year, month)
    n_weeks   = len(cal_weeks)
    today     = date.today()

    # Geometry
    WEEK_COL_W = 8 * mm           # narrow week-number gutter
    WKDAY_W    = (CONTENT_W - WEEK_COL_W) / 7   # each day column

    DOW_ROW_H  = 7 * mm           # height of day-of-week header row
    grid_top   = sep_y - 2 * mm
    grid_bot   = MARGIN
    body_h     = grid_top - DOW_ROW_H - grid_bot
    cell_h     = body_h / n_weeks

    # ── Day-of-week header row ──────────────────────────────────────────────
    dow_y  = grid_top - DOW_ROW_H
    # "Wk" label over the gutter
    txt(c, MARGIN, dow_y + 2 * mm, "WK",
        size=5.5, bold=True, col=C_SILVER)

    for col, label in enumerate(_DOW):
        cx = MARGIN + WEEK_COL_W + col * WKDAY_W
        is_wknd = col >= 5
        # Weekend column shading (full height of body)
        if is_wknd:
            filled_rect(c, cx, grid_bot,
                        WKDAY_W, body_h,
                        fill=C_WKND)
        txt(c, cx + WKDAY_W / 2, dow_y + 1.8 * mm, label,
            size=6, bold=True,
            col=C_GREY if is_wknd else C_INK_2,
            align="center")

    # Separator under header
    hrule(c, MARGIN, dow_y, CONTENT_W, col=C_SILVER, lw=0.5)

    # ── Calendar cells ──────────────────────────────────────────────────────
    for row, week in enumerate(cal_weeks):
        row_y_top = dow_y - row * cell_h
        row_y_bot = row_y_top - cell_h

        # Week number (ISO)
        if week[0] or week[-1]:
            # Find any non-zero day to get the week number
            sample_day = next(d for d in week if d)
            wk_num = date(year, month, sample_day).isocalendar()[1]
            txt(c, MARGIN + 1 * mm, row_y_bot + cell_h - 4 * mm,
                str(wk_num), size=5.5, col=C_SILVER)

        # Horizontal row divider
        hrule(c, MARGIN, row_y_bot, CONTENT_W, col=C_GHOST, lw=0.3)

        for col, day_num in enumerate(week):
            if day_num == 0:
                continue

            cx    = MARGIN + WEEK_COL_W + col * WKDAY_W
            is_wknd   = col >= 5
            is_today  = (date(year, month, day_num) == today)
            day_key   = f"{year}-{month:02d}-{day_num:02d}"

            # ── Day number ─────────────────────────────────────────
            num_cx = cx + 4 * mm
            num_cy = row_y_top - 4.5 * mm

            if is_today:
                # Filled circle behind the number
                circle(c, num_cx, num_cy + 1.5 * mm, 4.5 * mm, fill=C_ACCENT)
                txt(c, num_cx, num_cy, str(day_num),
                    size=8, bold=True, col=C_WHITE, align="center")
            else:
                num_col = C_GREY if is_wknd else C_INK
                txt(c, num_cx, num_cy, str(day_num),
                    size=8, bold=False, col=num_col, align="center")

            # ── Event pills ────────────────────────────────────────
            day_evts = ev_by_day.get(day_num, [])
            pill_x   = cx + 1 * mm
            pill_w   = WKDAY_W - 2 * mm
            pill_h   = 4.2 * mm
            pill_gap = 1.2 * mm
            # Start below the day number area
            pill_y   = row_y_top - 10 * mm

            for i, evt in enumerate(day_evts[:3]):
                if pill_y - pill_h < row_y_bot + 1 * mm:
                    # No room — show overflow dot
                    circle(c, cx + WKDAY_W - 3 * mm,
                           row_y_bot + 3 * mm, 1.5 * mm,
                           fill=C_SILVER)
                    break
                ec = _event_color(evt.title)
                filled_rect(c, pill_x, pill_y - pill_h,
                            pill_w, pill_h, fill=ec, r=1.5)
                txt(c, pill_x + 2 * mm, pill_y - pill_h + 1.3 * mm,
                    evt.title, size=5, bold=True, col=C_WHITE,
                    max_w=pill_w - 3 * mm)
                pill_y -= pill_h + pill_gap

            # Tap target → weekly page
            if day_key in day_week_map:
                c.linkAbsolute("", day_week_map[day_key],
                               (cx, row_y_bot, cx + WKDAY_W, row_y_top))

        # Vertical column separators (draw once per row, over the content)
        for col in range(1, 7):
            vx = MARGIN + WEEK_COL_W + col * WKDAY_W
            vrule(c, vx, row_y_bot, cell_h, col=C_GHOST, lw=0.3)

    # Outer grid borders
    hrule(c, MARGIN, grid_bot, CONTENT_W, col=C_GHOST, lw=0.3)
    vrule(c, MARGIN + WEEK_COL_W, grid_bot, body_h, col=C_GHOST, lw=0.3)
    vrule(c, MARGIN + WEEK_COL_W + 5 * WKDAY_W, grid_bot,
          body_h, col=C_SILVER, lw=0.5)   # weekday/weekend divider


# ─────────────────────────────────────────────────────────────────────────────
# Weekly spread page
# ─────────────────────────────────────────────────────────────────────────────

def draw_week_page(c: canvas.Canvas,
                   week_monday: date,
                   days_events: dict,
                   event_page_map: dict,
                   tz):
    """
    5-column Mon–Fri weekly spread with timed event blocks.
    """
    week_friday = week_monday + timedelta(days=4)
    month       = week_monday.month
    week_num    = week_monday.isocalendar()[1]

    draw_tab(c, month)

    # Header
    month_str = week_monday.strftime("%B").upper()
    year_str  = str(week_monday.year)
    # Combined "MONTH YEAR" in two weights
    label = f"{month_str} {year_str}"

    if week_monday.month == week_friday.month:
        date_range = (f"WEEK {week_num}  ·  "
                      f"{week_monday.day}–{week_friday.day} "
                      f"{week_monday.strftime('%b').upper()}")
    else:
        date_range = (f"WEEK {week_num}  ·  "
                      f"{week_monday.day} {week_monday.strftime('%b').upper()} – "
                      f"{week_friday.day} {week_friday.strftime('%b').upper()}")

    sep_y = draw_page_header(
        c,
        left_label=label,
        left_sub=date_range,
        right_label="WEEKLY PLAN",
        right_sub=str(week_num),
        accent_bar=True,
    )

    # ── Grid geometry ────────────────────────────────────────────────────────
    TIME_COL_W = 9 * mm
    DAY_COL_W  = (CONTENT_W - TIME_COL_W) / 5
    BOTTOM_H   = 40 * mm          # reserved for FOCUS/PRIORITIES/NOTES
    DAY_HDR_H  = 13 * mm          # day-number header above the time grid

    grid_top = sep_y - DAY_HDR_H
    grid_bot = MARGIN + BOTTOM_H
    grid_h   = grid_top - grid_bot

    n_hours  = HOUR_END - HOUR_START
    slot_h   = grid_h / n_hours

    today = date.today()
    days  = [week_monday + timedelta(days=i) for i in range(5)]

    # ── Day column headers ───────────────────────────────────────────────────
    hdr_top = sep_y - 1 * mm   # top of the day-header band

    for i, day in enumerate(days):
        cx      = MARGIN + TIME_COL_W + i * DAY_COL_W
        is_td   = (day == today)
        num_str = str(day.day)
        abbr    = day.strftime("%a").upper()

        # Column separator (skip first)
        if i > 0:
            vrule(c, cx, grid_bot, grid_h + DAY_HDR_H, col=C_GHOST, lw=0.4)

        # Today column highlight
        if is_td:
            filled_rect(c, cx, grid_bot, DAY_COL_W, grid_h + DAY_HDR_H,
                        fill=C_ACCENT_LT)

        # Day abbreviation (small, grey, top)
        txt(c, cx + DAY_COL_W / 2, hdr_top - 5 * mm, abbr,
            size=6, bold=False, col=C_GREY, align="center")

        # Day number (large)
        num_col = C_ACCENT if is_td else C_INK
        txt(c, cx + DAY_COL_W / 2, hdr_top - 12 * mm, num_str,
            size=14, bold=True, col=num_col, align="center")

    # Separator between day headers and time grid
    hrule(c, MARGIN + TIME_COL_W, grid_top, CONTENT_W - TIME_COL_W,
          col=C_SILVER, lw=0.5)

    # ── Time grid ────────────────────────────────────────────────────────────
    for i in range(n_hours + 1):
        hour  = HOUR_START + i
        y     = grid_top - i * slot_h

        # Hour label
        if i < n_hours:
            lbl = f"{hour:02d}:00"
            txt(c, MARGIN + TIME_COL_W - 1 * mm, y - 3, lbl,
                size=5.5, col=C_SILVER, align="right")

        # Full-width hour rule
        hrule(c, MARGIN + TIME_COL_W, y, CONTENT_W - TIME_COL_W,
              col=C_GHOST, lw=0.4)

        # 30-min half rule (even lighter)
        if i < n_hours:
            hrule(c, MARGIN + TIME_COL_W, y - slot_h / 2,
                  CONTENT_W - TIME_COL_W,
                  col=colors.HexColor("#F3F3F5"), lw=0.25)

    # ── Event blocks ─────────────────────────────────────────────────────────
    for di, day in enumerate(days):
        day_key = day.strftime("%Y-%m-%d")
        evts    = sorted(days_events.get(day_key, []), key=lambda e: e.start)
        cx      = MARGIN + TIME_COL_W + di * DAY_COL_W

        # All-day strip (thin bar at the very top of the column header)
        all_day = [e for e in evts if e.is_all_day]
        for ai, ade in enumerate(all_day[:2]):
            bar_y = hdr_top - 4.5 * mm - ai * 4.2 * mm
            filled_rect(c, cx + 0.5, bar_y - 3.2 * mm,
                        DAY_COL_W - 1, 3.2 * mm,
                        fill=_event_color(ade.title), r=1)
            txt(c, cx + 2 * mm, bar_y - 3.2 * mm + 0.8 * mm,
                ade.title, size=4.5, bold=True, col=C_WHITE,
                max_w=DAY_COL_W - 4 * mm)

        # Timed events — simple lane-based overlap split
        timed = [e for e in evts if not e.is_all_day]
        lanes: list[list] = []
        for evt in timed:
            placed = False
            for lane in lanes:
                if evt.start.astimezone(tz) >= lane[-1].end.astimezone(tz):
                    lane.append(evt); placed = True; break
            if not placed:
                lanes.append([evt])

        n_lanes = max(len(lanes), 1)
        for li, lane in enumerate(lanes):
            lx = cx + li * (DAY_COL_W / n_lanes) + 0.5
            lw = DAY_COL_W / n_lanes - 1

            for evt in lane:
                ls = evt.start.astimezone(tz)
                le = evt.end.astimezone(tz)
                if ls.hour >= HOUR_END or le.hour < HOUR_START:
                    continue

                sf = max(ls.hour + ls.minute / 60, HOUR_START) - HOUR_START
                ef = min(le.hour + le.minute / 60, HOUR_END)   - HOUR_START
                bh = (ef - sf) * slot_h - 1.5
                bt = grid_top - sf * slot_h        # block top (y of top edge)
                bb = bt - bh                        # block bottom

                ec = _event_color(evt.title)

                # Block
                filled_rect(c, lx, bb, lw, bh, fill=ec, r=2)

                # Title
                if bh > 5 * mm:
                    txt(c, lx + 2 * mm, bt - 4 * mm,
                        evt.title, size=5.5, bold=True, col=C_WHITE,
                        max_w=lw - 3 * mm)
                # Time
                if bh > 10 * mm:
                    t_str = f"{ls.strftime('%H:%M')}–{le.strftime('%H:%M')}"
                    txt(c, lx + 2 * mm, bt - 9 * mm,
                        t_str, size=4.5, col=C_WHITE, max_w=lw - 3 * mm)

                # Tap → meeting note
                if evt.id in event_page_map:
                    c.linkAbsolute("", f"event_{evt.id}",
                                   (lx, bb, lx + lw, bt))

    # ── Bottom sections: FOCUS | PRIORITIES | NOTES ──────────────────────────
    _draw_bottom_sections(c, MARGIN, MARGIN + BOTTOM_H, CONTENT_W)


def _draw_bottom_sections(c: canvas.Canvas,
                           x: float, top_y: float, width: float):
    """
    Three equal-width sections below the time grid.
    FOCUS (lined)  |  PRIORITIES (numbered)  |  NOTES (lined)
    """
    sec_w  = width / 3
    gap_y  = 1.5 * mm    # gap between label and first line
    line_h = 6.5 * mm    # vertical spacing of writing lines
    bot    = MARGIN + 1 * mm

    hrule(c, x, top_y, width, col=C_SILVER, lw=0.5)   # top boundary

    for idx, (label, numbered) in enumerate([
            ("FOCUS", False), ("PRIORITIES", True), ("NOTES", False)]):
        sx = x + idx * sec_w

        # Vertical dividers between sections
        if idx > 0:
            vrule(c, sx, MARGIN, top_y - MARGIN, col=C_GHOST, lw=0.4)

        # Section label
        txt(c, sx + 2 * mm, top_y - 5 * mm, label,
            size=6, bold=True, col=C_GREY, italic=False)

        # Writing lines
        ly = top_y - 5 * mm - gap_y - 1.5 * mm
        n  = 1
        while ly > bot:
            lx2 = sx + sec_w - 2 * mm
            if numbered:
                txt(c, sx + 2 * mm, ly, str(n), size=6, col=C_SILVER)
                hrule(c, sx + 6 * mm, ly - 1 * mm, sec_w - 8 * mm,
                      col=C_GHOST, lw=0.35)
                n += 1
            else:
                hrule(c, sx + 2 * mm, ly - 1 * mm, sec_w - 4 * mm,
                      col=C_GHOST, lw=0.35)
            ly -= line_h


# ─────────────────────────────────────────────────────────────────────────────
# Meeting note page
# ─────────────────────────────────────────────────────────────────────────────

def _time_label(evt, tz) -> str:
    if evt.is_all_day:
        return "All day"
    s = evt.start.astimezone(tz)
    e = evt.end.astimezone(tz)
    return f"{s.strftime('%H:%M')} – {e.strftime('%H:%M')}"


def draw_meeting_page(c: canvas.Canvas, event: CalendarEvent,
                      week_bookmark: str, tz=None):
    tz  = tz or timezone.utc
    ls  = event.start.astimezone(tz)
    month = ls.month

    draw_tab(c, month)

    day_label  = ls.strftime("%A %-d %B %Y")
    time_label = _time_label(event, tz)

    sep_y = draw_page_header(
        c,
        left_label=event.title,
        left_sub=day_label,
        right_label=time_label,
        right_sub=event.duration_str,
        accent_bar=True,
    )

    # Back link
    c.setFont("Helvetica", 7)
    c.setFillColor(C_ACCENT)
    back_x = MARGIN + CONTENT_W
    back_y = PAGE_H - MARGIN - 20
    c.drawRightString(back_x, back_y, "← Week")
    c.linkAbsolute("", week_bookmark,
                   (back_x - 18 * mm, back_y - 1 * mm,
                    back_x, back_y + 5 * mm))

    y = sep_y - 6 * mm

    # ── Metadata chips ────────────────────────────────────────────────────────
    meta = [("Duration", event.duration_str)]
    if event.location:      meta.append(("Location", event.location))
    if event.calendar_name: meta.append(("Calendar", event.calendar_name))

    chip_x = MARGIN
    chip_h = 6 * mm
    chip_pad = 3 * mm
    for lbl, val in meta:
        text     = f"{lbl}: {val}"
        font     = "Helvetica"
        chip_w   = c.stringWidth(text, font, 7) + 2 * chip_pad
        if chip_x + chip_w > MARGIN + CONTENT_W:
            chip_x = MARGIN; y -= chip_h + 2.5 * mm
        filled_rect(c, chip_x, y - chip_h, chip_w, chip_h,
                    fill=C_ACCENT_LT, r=3)
        c.setFont(font, 7)
        c.setFillColor(C_INK_2)
        c.drawString(chip_x + chip_pad, y - chip_h + 1.8 * mm, text)
        chip_x += chip_w + 2 * mm

    y -= chip_h + 6 * mm

    # ── Attendees ─────────────────────────────────────────────────────────────
    if event.attendees:
        hrule(c, MARGIN, y, CONTENT_W, col=C_GHOST, lw=0.4)
        y -= 5 * mm
        txt(c, MARGIN, y, "Attendees", size=7.5, bold=True, col=C_INK_2)
        y -= 6 * mm

        _RESP = {
            "accepted":  colors.HexColor("#4CAF50"),
            "tentative": colors.HexColor("#FF9800"),
            "declined":  colors.HexColor("#F44336"),
        }
        att_x = MARGIN
        for att in event.attendees:
            label = att.name or att.email
            dot_c = _RESP.get(att.response, C_SILVER)
            aw = c.stringWidth(label, "Helvetica", 8) + 8 * mm
            if att_x + aw > MARGIN + CONTENT_W:
                att_x = MARGIN; y -= 6 * mm
            circle(c, att_x + 1.5 * mm, y + 2 * mm, 1.8 * mm, fill=dot_c)
            txt(c, att_x + 5 * mm, y, label, size=8, col=C_INK_2)
            att_x += aw + 3 * mm
        y -= 8 * mm

    # ── Agenda / description ──────────────────────────────────────────────────
    if event.description:
        hrule(c, MARGIN, y, CONTENT_W, col=C_GHOST, lw=0.4)
        y -= 5 * mm
        txt(c, MARGIN, y, "Agenda", size=7.5, bold=True, col=C_INK_2)
        y -= 6 * mm
        words = event.description.split()
        line  = ""
        for word in words:
            test = (line + " " + word).strip()
            if c.stringWidth(test, "Helvetica", 7.5) < CONTENT_W:
                line = test
            else:
                if y < MARGIN + 35 * mm: break
                txt(c, MARGIN, y, line, size=7.5, col=C_INK_2)
                y -= 5 * mm; line = word
        if line and y > MARGIN + 35 * mm:
            txt(c, MARGIN, y, line, size=7.5, col=C_INK_2)
            y -= 8 * mm

    # ── Notes section ─────────────────────────────────────────────────────────
    hrule(c, MARGIN, y, CONTENT_W, col=C_SILVER, lw=0.5)
    y -= 5 * mm
    txt(c, MARGIN, y, "Notes", size=7.5, bold=True, col=C_INK_2)

    # Accent bar to the left of the writing area
    filled_rect(c, MARGIN, MARGIN, 2, y - 4 * mm - MARGIN,
                fill=colors.HexColor(MONTH_TAB_COLORS[(event.start.astimezone(tz).month - 1)]))

    y -= 8 * mm
    while y > MARGIN + 6 * mm:
        hrule(c, MARGIN + 4 * mm, y, CONTENT_W - 4 * mm,
              col=C_GHOST, lw=0.4)
        y -= 8.5 * mm


# ─────────────────────────────────────────────────────────────────────────────
# PDF builder
# ─────────────────────────────────────────────────────────────────────────────

def build_planner(
    events: list,
    output_path: str,
    start_date: date = None,
    end_date:   date = None,
    title:      str  = "Planner",
    timezone_name: str = "UTC",
):
    """
    Builds the hyperlinked PDF.

    Page order:
      1. Monthly overview pages (one per month in range)
      2. Weekly spread pages   (one per ISO week in range)
      3. Meeting note pages    (one per timed event)
    """
    if not start_date: start_date = date.today()
    if not end_date:   end_date   = start_date + timedelta(days=30)

    try:   tz = ZoneInfo(timezone_name)
    except Exception: tz = ZoneInfo("UTC")

    # Months
    months = []
    cur = date(start_date.year, start_date.month, 1)
    while cur <= end_date:
        months.append((cur.year, cur.month))
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)

    # Weeks (Mon–Fri)
    first_mon = _week_monday(start_date)
    last_mon  = _week_monday(end_date)
    weeks = []
    w = first_mon
    while w <= last_mon:
        weeks.append(w)
        w += timedelta(days=7)

    # Group events by month and by day
    ev_by_month: dict[tuple, list] = {m: [] for m in months}
    ev_by_day:   dict[str, list]   = {}
    for e in events:
        loc = e.start.astimezone(tz)
        key = (loc.year, loc.month)
        if key in ev_by_month:
            ev_by_month[key].append(e)
        dk = loc.strftime("%Y-%m-%d")
        ev_by_day.setdefault(dk, []).append(e)

    # day_week_map: "YYYY-MM-DD" → week bookmark key
    day_week_map: dict[str, str] = {}
    for w in weeks:
        bm = f"week_{w.isoformat()}"
        for i in range(5):
            dk = (w + timedelta(days=i)).strftime("%Y-%m-%d")
            day_week_map[dk] = bm

    # ── Pass 1: assign page numbers ──────────────────────────────────────────
    pg = 1
    for _ in months: pg += 1          # one page per month
    month_start_pg = 1
    week_start_pg  = len(months) + 1

    week_pg: dict[str, int] = {}
    for i, w in enumerate(weeks):
        week_pg[w.isoformat()] = week_start_pg + i
    pg = week_start_pg + len(weeks)

    # Meeting note pages — one per timed event that falls in our day_week_map
    timed_events = [
        e for e in events
        if not e.is_all_day
        and e.start.astimezone(tz).strftime("%Y-%m-%d") in day_week_map
    ]
    timed_events.sort(key=lambda e: e.start)

    event_pg:    dict[str, int] = {}
    event_wk_bm: dict[str, str] = {}
    for e in timed_events:
        event_pg[e.id] = pg
        dk = e.start.astimezone(tz).strftime("%Y-%m-%d")
        event_wk_bm[e.id] = day_week_map[dk]
        pg += 1

    total_pages = pg - 1

    # ── Pass 2: draw pages ───────────────────────────────────────────────────
    c = canvas.Canvas(output_path, pagesize=A4)
    c.setTitle(title)
    c.setAuthor("PolarisFolio")

    # Monthly overviews
    for year, month in months:
        bm = f"month_{year}_{month:02d}"
        c.bookmarkPage(bm)
        c.addOutlineEntry(
            datetime(year, month, 1).strftime("%B %Y"), bm, level=0)
        draw_month_page(c, year, month, ev_by_month[(year, month)], day_week_map)
        c.showPage()

    # Weekly spreads
    for w in weeks:
        bm = f"week_{w.isoformat()}"
        c.bookmarkPage(bm)
        fri = w + timedelta(days=4)
        c.addOutlineEntry(
            f"Week {w.isocalendar()[1]} · {w.strftime('%-d %b')} – {fri.strftime('%-d %b')}",
            bm, level=1)
        wk_evts = {
            (w + timedelta(days=i)).strftime("%Y-%m-%d"):
            ev_by_day.get((w + timedelta(days=i)).strftime("%Y-%m-%d"), [])
            for i in range(5)
        }
        draw_week_page(c, w, wk_evts, event_pg, tz)
        c.showPage()

    # Meeting notes
    for e in timed_events:
        bm = f"event_{e.id}"
        c.bookmarkPage(bm)
        c.addOutlineEntry(f"  {e.title[:40]}", bm, level=2)
        draw_meeting_page(c, e, event_wk_bm[e.id], tz=tz)
        c.showPage()

    c.save()
    print(f"PDF saved: {output_path}  ({total_pages} pages)")
    return output_path


# ─────────────────────────────────────────────────────────────────────────────
# Quick self-test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from models import CalendarEvent, Attendee

    today = date.today()

    def _evt(day_off, hour, dur_h, title, loc=None, desc=None, atts=None):
        d  = today + timedelta(days=day_off)
        s  = datetime(d.year, d.month, d.day, hour,          0, tzinfo=timezone.utc)
        e  = datetime(d.year, d.month, d.day, hour + dur_h,  0, tzinfo=timezone.utc)
        return CalendarEvent(
            id=f"t{day_off}_{hour}", title=title,
            start=s, end=e, location=loc, description=desc,
            attendees=atts or [], calendar_name="AD Calendar", source="ical")

    test_events = [
        _evt(0,  9, 1, "Team Standup", "Teams",
             atts=[Attendee("Elliot L", "e@ad.com.au", "accepted")]),
        _evt(0, 10, 2, "Azure QBR", "Level 7, 123 Eagle St",
             desc="Quarterly business review with Azure team.",
             atts=[Attendee("Tom Deakin", "tom@azd.com.au", "accepted"),
                   Attendee("Genna Boylan", "genna@ad.com.au", "tentative")]),
        _evt(0, 14, 1, "1:1 with Peter"),
        _evt(1,  9, 1, "L10 Weekly", "Boardroom"),
        _evt(1, 10, 2, "Halo Meeting", "Teams"),
        _evt(2, 11, 1, "Elementa Markets Review", "Teams"),
        _evt(3,  9, 2, "Superior Engineering Onsite", "Superior HQ"),
        _evt(4,  9, 1, "Team Standup"),
        _evt(4, 11, 2, "Life Fertility IT Committee", "North Lakes"),
        _evt(7,  9, 1, "Team Standup"),
        _evt(7, 15, 1, "SDM Sync", "Boardroom"),
        _evt(10, 9, 1, "Team Standup"),
        _evt(11,15, 1, "1:1 with Aaron"),
    ]

    out_dir = os.path.expanduser("~/polarisfolio_pdfs")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "test_weekly.pdf")

    build_planner(
        test_events, out,
        start_date=today,
        end_date=today + timedelta(days=13),
        title="PolarisFolio Test",
        timezone_name="Australia/Brisbane",
    )
