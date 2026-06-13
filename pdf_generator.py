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
C_GHOST     = colors.HexColor("#AAAAAA")   # lightest rule / cell border (bumped for e-ink)
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
# Navigation buttons (top-right of every page header)
# ─────────────────────────────────────────────────────────────────────────────

YEAR_BM = "year_overview"   # bookmark for the year overview page

C_WHITE = colors.white      # convenience alias used in nav icon drawing


def _draw_nav_icon(c: canvas.Canvas, cx: float, cy: float,
                   sz: float, name: str, col):
    """Draw one nav icon centred at (cx, cy) within a sz×sz logical box."""
    if name == "year":
        # 3×3 grid of tiny squares
        dot = sz * 0.13
        step = sz * 0.33
        for row in range(3):
            for ci in range(3):
                dx = cx + (ci - 1) * step
                dy = cy + (row - 1) * step
                c.setFillColor(col)
                c.rect(dx - dot / 2, dy - dot / 2, dot, dot, fill=1, stroke=0)

    elif name in ("month", "day"):
        w = sz * 0.64; h = sz * 0.58; hdr_h = h * 0.3
        x = cx - w / 2; y = cy - h / 2
        c.setStrokeColor(col); c.setFillColor(C_WHITE); c.setLineWidth(0.5)
        c.rect(x, y, w, h, fill=1, stroke=1)
        c.setFillColor(col)
        c.rect(x, y + h - hdr_h, w, hdr_h, fill=1, stroke=0)
        if name == "day":
            sq = sz * 0.15
            c.rect(cx - sq / 2, cy - h * 0.1, sq, sq, fill=1, stroke=0)

    elif name == "week":
        w = sz * 0.64; h = sz * 0.58; hdr_h = h * 0.3
        x = cx - w / 2; y = cy - h / 2
        c.setStrokeColor(col); c.setFillColor(C_WHITE); c.setLineWidth(0.5)
        c.rect(x, y, w, h, fill=1, stroke=1)
        c.setFillColor(col)
        c.rect(x, y + h - hdr_h, w, hdr_h, fill=1, stroke=0)
        c.setStrokeColor(col); c.setLineWidth(0.3)
        body_top = y + h - hdr_h
        for i in range(1, 5):
            lx = x + w * i / 5
            c.line(lx, y, lx, body_top)

    elif name == "menu":
        lw = sz * 0.56
        c.setStrokeColor(col); c.setLineWidth(0.75)
        for row in range(3):
            ly = cy + (row - 1) * sz * 0.29
            c.line(cx - lw / 2, ly, cx + lw / 2, ly)


def draw_nav_buttons(c: canvas.Canvas, active: str,
                     month_bm: str = "", week_bm: str = "", day_bm: str = ""):
    """
    Draw 5 nav icons in the top-right of the page header.
    active: "year" | "month" | "week" | "day"
    Icons (left→right): year-grid, month-cal, week-cal, day-cal, menu
    Active icon uses C_ACCENT; others use C_SILVER.
    Tappable icons link to their bookmark; no-bookmark icons are display-only.
    """
    SZ    = 6.5 * mm
    GAP   = 2.0 * mm
    right = MARGIN + CONTENT_W
    top   = PAGE_H - MARGIN
    cy    = top - 13   # vertically centred in the ~26pt header band

    icons = [
        ("year",  YEAR_BM),
        ("month", month_bm),
        ("week",  week_bm),
        ("day",   day_bm),
        ("menu",  ""),
    ]

    total_w = len(icons) * SZ + (len(icons) - 1) * GAP
    start_x = right - total_w

    for i, (name, bm) in enumerate(icons):
        cx = start_x + i * (SZ + GAP) + SZ / 2
        col = C_ACCENT if name == active else C_SILVER
        _draw_nav_icon(c, cx, cy, SZ, name, col)
        if bm:
            lx = cx - SZ / 2
            ly = cy - SZ / 2
            c.linkAbsolute("", bm, (lx, ly, lx + SZ, ly + SZ))


# ─────────────────────────────────────────────────────────────────────────────
# Right-edge month tab
# ─────────────────────────────────────────────────────────────────────────────

def draw_tab(c: canvas.Canvas, month: int, month_bookmark: str = ""):
    """Coloured right-edge tab with rotated month abbreviation.
    If month_bookmark is given the tab becomes a tap target back to that month page."""
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
    c.setFont("Helvetica-Bold", 8.5)
    c.translate(tab_x + TAB_W / 2, PAGE_H / 2)
    c.rotate(90)
    c.drawCentredString(0, -2.5, abbr)
    c.restoreState()

    # Tap target — entire tab navigates back to the monthly overview
    if month_bookmark:
        c.linkAbsolute("", month_bookmark, (tab_x, 0, PAGE_W, PAGE_H))


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
# Year overview page
# ─────────────────────────────────────────────────────────────────────────────

_MINI_DOW = ["S", "M", "T", "W", "T", "F", "S"]

def draw_year_page(c: canvas.Canvas, year: int, day_week_map: dict,
                   active_months: set = None, tz=timezone.utc):
    """
    Full-year calendar: 3-col × 4-row mini-month grid.
    All 12 month tabs are shown as equal-height segments on the right edge.
    Each tab segment links to that month's overview page.
    Each date cell links to its weekly spread.
    Today is circled; the current month name gets a colour pill badge.
    """
    today = datetime.now(tz).date()

    # ── All-12-months right-edge tabs ─────────────────────────────────────────
    tab_x   = PAGE_W - TAB_W
    seg_h   = PAGE_H / 12
    for m in range(1, 13):
        fill_col = colors.HexColor(MONTH_TAB_COLORS[(m - 1) % 12])
        y0 = PAGE_H - m * seg_h
        filled_rect(c, tab_x, y0, TAB_W, seg_h, fill=fill_col)
        # Each tab segment links to that month (only if it has a page)
        if active_months is None or m in active_months:
            mbm = f"month_{year}_{m:02d}"
            c.linkAbsolute("", mbm, (tab_x, y0, PAGE_W, y0 + seg_h))
    vrule(c, tab_x, 0, PAGE_H, col=colors.HexColor("#00000018"), lw=0.5)

    # ── Page header ───────────────────────────────────────────────────────────
    top = PAGE_H - MARGIN
    # "2026" bold + "CALENDAR" light
    c.setFont("Helvetica-Bold", 15)
    c.setFillColor(C_INK)
    year_str = str(year)
    c.drawString(MARGIN + 6, top - 11, year_str)
    year_w = c.stringWidth(year_str, "Helvetica-Bold", 15)
    txt(c, MARGIN + 6 + year_w + 4, top - 11, "CALENDAR",
        size=15, bold=False, col=C_INK_2)

    sep_y = top - 26
    hrule(c, MARGIN, sep_y, CONTENT_W, col=C_SILVER, lw=0.6)

    draw_nav_buttons(c, "year")

    # ── Mini-month grid ───────────────────────────────────────────────────────
    COL_GAP  = 5 * mm
    ROW_GAP  = 4 * mm
    avail_w  = CONTENT_W
    avail_h  = sep_y - MARGIN
    cell_w   = (avail_w - 2 * COL_GAP) / 3
    cell_h   = (avail_h - 3 * ROW_GAP) / 4

    for m in range(1, 13):
        row  = (m - 1) // 3
        col  = (m - 1) % 3
        cx0  = MARGIN + col * (cell_w + COL_GAP)
        # top-y of this cell
        cell_top = sep_y - ROW_GAP - row * (cell_h + ROW_GAP)

        is_cur = (m == today.month and year == today.year)
        tab_col = MONTH_TAB_COLORS[(m - 1) % 12]

        # Month name
        mname  = datetime(year, m, 1).strftime("%B").upper()
        name_y = cell_top - 5.5 * mm

        if is_cur:
            pw = c.stringWidth(mname, "Helvetica-Bold", 7) + 4 * mm
            pill_x = cx0 + cell_w / 2 - pw / 2
            filled_rect(c, pill_x, name_y - 1.5 * mm, pw, 5.5 * mm,
                        fill=colors.HexColor(tab_col), r=2)
            txt(c, cx0 + cell_w / 2, name_y, mname,
                size=7, bold=True, col=C_WHITE, align="center")
        else:
            txt(c, cx0 + cell_w / 2, name_y, mname,
                size=7, bold=True, col=C_INK, align="center")

        # DOW row
        dow_y   = name_y - 5.5 * mm
        cell_sz = cell_w / 7
        for di, d in enumerate(_MINI_DOW):
            dx = cx0 + di * cell_sz + cell_sz / 2
            col_d = C_GREY if di >= 5 else C_INK_2
            txt(c, dx, dow_y, d, size=6, col=col_d, align="center")

        # Thin rule under DOW
        hrule(c, cx0, dow_y - 1 * mm, cell_w, col=C_GHOST, lw=0.5)

        # Date cells
        first   = date(year, m, 1)
        dow0    = (first.weekday() + 1) % 7   # 0=Sun
        _, n_days = calendar.monthrange(year, m)
        date_y0 = dow_y - 5 * mm   # y of the first date row

        for day_num in range(1, n_days + 1):
            slot      = dow0 + day_num - 1
            grid_row  = slot // 7
            grid_col  = slot % 7
            dx = cx0 + grid_col * cell_sz + cell_sz / 2
            dy = date_y0 - grid_row * 4.2 * mm

            is_td   = (day_num == today.day and m == today.month
                       and year == today.year)
            is_wknd = (grid_col == 0 or grid_col == 6)   # Sun or Sat

            if is_td:
                circle(c, dx, dy + 1.5 * mm, 2.8 * mm,
                       fill=C_INK)
                txt(c, dx, dy, str(day_num), size=6.5, bold=True,
                    col=C_WHITE, align="center")
            else:
                num_col = C_GREY if is_wknd else C_INK_2
                txt(c, dx, dy, str(day_num), size=6.5,
                    col=num_col, align="center")

            # Tap → week page
            day_key = date(year, m, day_num).strftime("%Y-%m-%d")
            if day_key in day_week_map:
                c.linkAbsolute("", day_week_map[day_key],
                               (dx - cell_sz / 2, dy - 0.8 * mm,
                                dx + cell_sz / 2, dy + 3.8 * mm))


# ─────────────────────────────────────────────────────────────────────────────
# Monthly overview page
# ─────────────────────────────────────────────────────────────────────────────

_DOW = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]

def draw_month_page(c: canvas.Canvas, year: int, month: int,
                    events: list,
                    day_week_map: dict,
                    tz=timezone.utc):
    """
    Full-page monthly calendar grid.

    Columns: week-num gutter + SUN … SAT
    Each cell: day number (circle if today) + up to 2 event pill bars
    """
    draw_tab(c, month)
    month_name = datetime(year, month, 1).strftime("%B").upper()
    sep_y = draw_page_header(
        c,
        left_label=f"{month_name} {year}",
        left_sub="MONTHLY OVERVIEW",
        accent_bar=True,
    )
    # First week of this month as the "week" nav target
    first_day_key = date(year, month, 1).strftime("%Y-%m-%d")
    week_bm = day_week_map.get(first_day_key, "")
    month_bm = f"month_{year}_{month:02d}"
    draw_nav_buttons(c, "month", month_bm=month_bm, week_bm=week_bm)

    # Group events by local day number
    ev_by_day: dict[int, list] = {}
    for e in events:
        loc = e.start.astimezone(tz)
        if loc.year == year and loc.month == month:
            ev_by_day.setdefault(loc.day, []).append(e)

    cal_weeks = calendar.Calendar(firstweekday=6).monthdayscalendar(year, month)
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
        size=7, bold=True, col=C_SILVER)

    for col, label in enumerate(_DOW):
        cx = MARGIN + WEEK_COL_W + col * WKDAY_W
        is_wknd = col == 0 or col == 6   # Sun or Sat
        # Weekend column shading (full height of body)
        if is_wknd:
            filled_rect(c, cx, grid_bot,
                        WKDAY_W, body_h,
                        fill=C_WKND)
        txt(c, cx + WKDAY_W / 2, dow_y + 1.8 * mm, label,
            size=7.5, bold=True,
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
                str(wk_num), size=7, col=C_SILVER)

        # Horizontal row divider
        hrule(c, MARGIN, row_y_bot, CONTENT_W, col=C_GHOST, lw=0.5)

        for col, day_num in enumerate(week):
            if day_num == 0:
                continue

            cx    = MARGIN + WEEK_COL_W + col * WKDAY_W
            is_wknd   = col == 0 or col == 6   # Sun or Sat
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
            pill_h   = 5.5 * mm
            pill_gap = 1.2 * mm
            # Start below the day number area
            pill_y   = row_y_top - 10 * mm

            for i, evt in enumerate(day_evts[:3]):
                if pill_y - pill_h < row_y_bot + 1 * mm:
                    # No room — show overflow dot
                    circle(c, cx + WKDAY_W - 3 * mm,
                           row_y_bot + 3 * mm, 2 * mm,
                           fill=C_SILVER)
                    break
                ec = _event_color(evt.title)
                filled_rect(c, pill_x, pill_y - pill_h,
                            pill_w, pill_h, fill=ec, r=2)
                txt(c, pill_x + 2 * mm, pill_y - pill_h + 1.8 * mm,
                    evt.title, size=8, bold=True, col=C_WHITE,
                    max_w=pill_w - 3 * mm)
                pill_y -= pill_h + pill_gap

            # Tap target → weekly page
            if day_key in day_week_map:
                c.linkAbsolute("", day_week_map[day_key],
                               (cx, row_y_bot, cx + WKDAY_W, row_y_top))

        # Vertical column separators (draw once per row, over the content)
        for col in range(1, 7):
            vx = MARGIN + WEEK_COL_W + col * WKDAY_W
            vrule(c, vx, row_y_bot, cell_h, col=C_GHOST, lw=0.5)

    # Outer grid borders
    hrule(c, MARGIN, grid_bot, CONTENT_W, col=C_GHOST, lw=0.5)
    vrule(c, MARGIN + WEEK_COL_W, grid_bot, body_h, col=C_GHOST, lw=0.5)
    vrule(c, MARGIN + WEEK_COL_W + 5 * WKDAY_W, grid_bot,
          body_h, col=C_SILVER, lw=0.5)   # weekday/weekend divider


# ─────────────────────────────────────────────────────────────────────────────
# Weekly spread page
# ─────────────────────────────────────────────────────────────────────────────

def draw_week_page(c: canvas.Canvas,
                   week_monday: date,
                   days_events: dict,
                   event_page_map: dict,
                   tz,
                   month_bookmark: str = ""):
    """
    5-column Mon–Fri weekly spread with timed event blocks.
    Matches Dayfolio layout: number-only headers, full-width all-day band,
    FOCUS/PRIORITIES/HABIT CHART/TO DO LIST/NOTES bottom sections.
    """
    week_friday = week_monday + timedelta(days=4)
    month       = week_monday.month
    week_num    = week_monday.isocalendar()[1]

    draw_tab(c, month, month_bookmark=month_bookmark)

    # Header
    month_str = week_monday.strftime("%B").upper()
    year_str  = str(week_monday.year)
    label     = f"{month_str} {year_str}"

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
        accent_bar=True,
    )
    week_bm = f"week_{week_monday.isoformat()}"
    # Day button always links to the Monday day page for this week
    first_day_bm = f"day_{week_monday.isoformat()}"
    draw_nav_buttons(c, "week",
                     month_bm=month_bookmark,
                     week_bm=week_bm,
                     day_bm=first_day_bm)

    # ── Grid geometry ────────────────────────────────────────────────────────
    TIME_COL_W = 12 * mm
    DAY_COL_W  = (CONTENT_W - TIME_COL_W) / 5
    BOTTOM_H   = 72 * mm          # FOCUS + PRIORITIES + TO DO LIST + NOTES
    DAY_HDR_H  = 16 * mm          # day-number header band height

    today = datetime.now(tz).date()
    days  = [week_monday + timedelta(days=i) for i in range(5)]

    # Pre-collect unique all-day events across the week
    all_day_evts: list = []
    _seen: set = set()
    for day in days:
        for e in days_events.get(day.strftime("%Y-%m-%d"), []):
            if e.is_all_day and e.title not in _seen:
                all_day_evts.append(e)
                _seen.add(e.title)
    n_allday   = min(len(all_day_evts), 3)
    ALLDAY_H   = n_allday * 4.5 * mm   # zone between header sep and time grid

    # Y coordinates (top-down)
    hdr_top      = sep_y - 1 * mm         # top of day-number band
    hdr_sep_y    = sep_y - DAY_HDR_H      # rule between header & all-day zone
    time_grid_top = hdr_sep_y - ALLDAY_H  # top of timed grid proper
    grid_bot     = MARGIN + BOTTOM_H
    grid_h       = time_grid_top - grid_bot

    n_hours = HOUR_END - HOUR_START
    slot_h  = grid_h / n_hours

    # ── Day column headers (number only, coloured dot) ───────────────────────
    for i, day in enumerate(days):
        cx    = MARGIN + TIME_COL_W + i * DAY_COL_W
        is_td = (day == today)

        # Column separator (skip first)
        if i > 0:
            vrule(c, cx, grid_bot, time_grid_top + DAY_HDR_H + ALLDAY_H - grid_bot,
                  col=C_GHOST, lw=0.4)

        # Today column highlight (full height incl header)
        if is_td:
            filled_rect(c, cx, grid_bot,
                        DAY_COL_W, time_grid_top + DAY_HDR_H + ALLDAY_H - grid_bot,
                        fill=C_ACCENT_LT)

        # Day number — large, centred, no abbreviation
        num_col = C_ACCENT if is_td else C_INK
        num_str = str(day.day)
        num_cx  = cx + DAY_COL_W / 2
        txt(c, num_cx, hdr_top - 11 * mm, num_str,
            size=16, bold=True, col=num_col, align="center")

        # Small coloured dot to upper-right of number (nav indicator)
        nw = c.stringWidth(num_str, "Helvetica-Bold", 16)
        dot_col = _event_color(day.strftime("%B"))   # consistent per month
        circle(c, num_cx + nw / 2 + 2.5 * mm,
               hdr_top - 6 * mm, 1.8 * mm, fill=dot_col)

        # Tap the day number header to jump to that day's page
        c.linkAbsolute("", f"day_{day.isoformat()}",
                       (cx, hdr_sep_y, cx + DAY_COL_W, hdr_top))

    # Separator rule below day numbers
    hrule(c, MARGIN + TIME_COL_W, hdr_sep_y, CONTENT_W - TIME_COL_W,
          col=C_SILVER, lw=0.5)

    # ── All-day event bands (full-width, stacked) ────────────────────────────
    ad_bar_h = 6 * mm
    for ai, ade in enumerate(all_day_evts):
        bar_bot = hdr_sep_y - (ai + 1) * (ad_bar_h + 1 * mm)
        filled_rect(c, MARGIN + TIME_COL_W, bar_bot,
                    CONTENT_W - TIME_COL_W, ad_bar_h,
                    fill=_event_color(ade.title), r=2)
        txt(c, MARGIN + TIME_COL_W + 3 * mm, bar_bot + 2 * mm,
            ade.title, size=8.5, bold=True, col=C_WHITE,
            max_w=CONTENT_W - TIME_COL_W - 6 * mm)

    # ── Time grid ────────────────────────────────────────────────────────────
    for i in range(n_hours + 1):
        hour = HOUR_START + i
        y    = time_grid_top - i * slot_h

        # Hour label — digit only, very faint, right-aligned into gutter
        if i < n_hours:
            txt(c, MARGIN + TIME_COL_W - 1.5 * mm, y - 4,
                str(hour), size=9, col=C_GREY, align="right")

        # Hour rule
        hrule(c, MARGIN + TIME_COL_W, y, CONTENT_W - TIME_COL_W,
              col=C_GHOST, lw=0.5)

        # 30-min half rule (dashed, lighter)
        if i < n_hours:
            c.setStrokeColor(colors.HexColor("#BBBBBB"))
            c.setLineWidth(0.3)
            c.setDash([2, 4])
            c.line(MARGIN + TIME_COL_W, y - slot_h / 2,
                   MARGIN + TIME_COL_W + CONTENT_W - TIME_COL_W, y - slot_h / 2)
            c.setDash([])

    # ── Timed event blocks ───────────────────────────────────────────────────
    for di, day in enumerate(days):
        day_key = day.strftime("%Y-%m-%d")
        evts    = sorted(days_events.get(day_key, []), key=lambda e: e.start)
        cx      = MARGIN + TIME_COL_W + di * DAY_COL_W

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
                bt = time_grid_top - sf * slot_h
                bb = bt - bh

                ec = _event_color(evt.title)
                filled_rect(c, lx, bb, lw, bh, fill=ec, r=2)

                if bh > 7 * mm:
                    txt(c, lx + 2 * mm, bt - 6 * mm,
                        evt.title, size=8.5, bold=True, col=C_WHITE,
                        max_w=lw - 3 * mm)
                if bh > 14 * mm:
                    t_str = f"{ls.strftime('%H:%M')}–{le.strftime('%H:%M')}"
                    txt(c, lx + 2 * mm, bt - 13 * mm,
                        t_str, size=7, col=C_WHITE, max_w=lw - 3 * mm)

                if evt.id in event_page_map:
                    c.linkAbsolute("", f"event_{evt.id}",
                                   (lx, bb, lx + lw, bt))

    # ── Bottom sections ───────────────────────────────────────────────────────
    _draw_bottom_sections(c, MARGIN, MARGIN + BOTTOM_H, CONTENT_W)


def _draw_bottom_sections(c: canvas.Canvas,
                           x: float, top_y: float, width: float):
    """
    Dayfolio-style bottom layout:

      ┌──────────────────┬──────────────────────────────────────┐
      │  FOCUS           │  PRIORITIES  (● 1  ● 2  ○ 3)        │
      │  (vertical text) │  HABIT CHART (5-col mini grid)       │
      ├──────────────────┴──────────────────────────────────────┤
      │  TO DO LIST                │  NOTES                      │
      └────────────────────────────┴─────────────────────────────┘
    """
    bot     = MARGIN
    total_h = top_y - bot

    # Row split: top 58% = FOCUS + PRIORITIES zone, bottom 42% = TO DO + NOTES
    split_y  = bot + total_h * 0.42
    top_h    = top_y - split_y

    FOCUS_W  = width * 0.37          # left portion of top row
    PRIO_X   = x + FOCUS_W
    PRIO_W   = width - FOCUS_W

    line_h   = 7.5 * mm              # ruled line spacing (comfortable for stylus)
    circ_r   = 3.8 * mm              # priority circle radius

    # ── Boundary rules ────────────────────────────────────────────────────────
    hrule(c, x, top_y,  width, col=C_SILVER, lw=0.6)   # top edge
    hrule(c, x, split_y, width, col=C_SILVER, lw=0.5)  # row divider

    # ── Vertical dividers ─────────────────────────────────────────────────────
    vrule(c, PRIO_X,        split_y, top_h,    col=C_GHOST, lw=0.5)  # FOCUS | PRIO
    vrule(c, x + width / 2, bot,     split_y - bot, col=C_GHOST, lw=0.5)  # TODO | NOTES

    # ── FOCUS — rotated vertical label, ruled lines ───────────────────────────
    # "FOCUS" rotated 90° reading bottom→top, centred vertically in the zone
    c.saveState()
    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(C_GREY)
    c.translate(x + 4.5 * mm, (top_y + split_y) / 2)
    c.rotate(90)
    c.drawCentredString(0, 0, "FOCUS")
    c.restoreState()

    # Ruled lines in the FOCUS writing area
    ly = top_y - 7.5 * mm
    while ly > split_y + 2 * mm:
        hrule(c, x + 9 * mm, ly, FOCUS_W - 11 * mm, col=C_GHOST, lw=0.5)
        ly -= line_h

    # ── PRIORITIES — numbered filled circles + writing lines ──────────────────
    txt(c, PRIO_X + 3 * mm, top_y - 5 * mm, "PRIORITIES",
        size=9, bold=True, col=C_GREY)

    circ_cx = PRIO_X + 7 * mm
    cy      = top_y - 13 * mm
    for n in range(1, 4):
        if cy - circ_r < split_y + 3 * mm:
            break
        # Filled circle: first two solid accent, third outlined
        if n <= 2:
            circle(c, circ_cx, cy, circ_r, fill=C_ACCENT)
            c.setFont("Helvetica-Bold", 8)
            c.setFillColor(C_WHITE)
        else:
            circle(c, circ_cx, cy, circ_r,
                   fill=C_GHOST, stroke=C_SILVER, lw=0.5)
            c.setFont("Helvetica-Bold", 8)
            c.setFillColor(C_SILVER)
        c.drawCentredString(circ_cx, cy - 3, str(n))
        # Ruled line beside circle
        line_x = circ_cx + circ_r + 2 * mm
        hrule(c, line_x, cy, PRIO_W - circ_r * 2 - 12 * mm,
              col=C_GHOST, lw=0.5)
        cy -= circ_r * 2 + 3 * mm

    # ── HABIT CHART — 5-col mini grid below priorities ────────────────────────
    if cy > split_y + 8 * mm:
        txt(c, PRIO_X + 3 * mm, cy + 1.5 * mm, "HABIT CHART",
            size=7.5, bold=True, col=C_SILVER)
        cy -= 5 * mm
        cell_sz  = 4 * mm
        cell_gap = 0.8 * mm
        grid_x   = PRIO_X + 3 * mm
        row_y    = cy
        while row_y > split_y + cell_sz + 1 * mm:
            for ci in range(5):
                cx2 = grid_x + ci * (cell_sz + cell_gap)
                c.setStrokeColor(C_GHOST)
                c.setLineWidth(0.5)
                c.rect(cx2, row_y - cell_sz, cell_sz, cell_sz, fill=0, stroke=1)
            row_y -= cell_sz + cell_gap

    # ── TO DO LIST (bottom-left) ──────────────────────────────────────────────
    txt(c, x + 2 * mm, split_y - 5 * mm, "TO DO LIST",
        size=9, bold=True, col=C_GREY)
    ly = split_y - 12 * mm
    while ly > bot + 2 * mm:
        hrule(c, x + 2 * mm, ly, width / 2 - 4 * mm, col=C_GHOST, lw=0.5)
        ly -= line_h

    # ── NOTES (bottom-right) ──────────────────────────────────────────────────
    txt(c, x + width / 2 + 2 * mm, split_y - 5 * mm, "NOTES",
        size=9, bold=True, col=C_GREY)
    ly = split_y - 12 * mm
    while ly > bot + 2 * mm:
        hrule(c, x + width / 2 + 2 * mm, ly, width / 2 - 4 * mm,
              col=C_GHOST, lw=0.5)
        ly -= line_h


# ─────────────────────────────────────────────────────────────────────────────
# Day view page
# ─────────────────────────────────────────────────────────────────────────────

_DAY_QUOTES = [
    ("Write it on your heart that every day is the best day in the year.",
     "Ralph Waldo Emerson"),
    ("The secret of getting ahead is getting started.", "Mark Twain"),
    ("Either you run the day or the day runs you.", "Jim Rohn"),
    ("Today is the only day you own.", "Unknown"),
    ("Do something today that your future self will thank you for.", "Unknown"),
    ("One day or day one — you decide.", "Unknown"),
    ("This is the day the Lord has made; let us rejoice and be glad in it.",
     "Psalms 118:24"),
]


def draw_day_page(c: canvas.Canvas, day_date: date, events: list,
                  event_page_map: dict, tz,
                  week_bookmark: str = "", month_bookmark: str = ""):
    """
    Dayfolio-style day view: left time-grid, right planning panel.
    Left  ~38%: hour slots 7AM–8PM with timed event blocks
    Right ~62%: Quote · FOCUS box · PRIORITIES · TO DO LIST · NOTES
    """
    today = datetime.now(tz).date()
    month = day_date.month

    draw_tab(c, month, month_bookmark=month_bookmark)

    # ── Header ────────────────────────────────────────────────────────────────
    day_label = day_date.strftime("%A, %B %-d").upper()
    year_label = str(day_date.year)
    is_today   = (day_date == today)

    sep_y = draw_page_header(
        c,
        left_label=day_label,
        left_sub=year_label,
        accent_bar=True,
    )

    if is_today:
        w = c.stringWidth(day_label, "Helvetica-Bold", 15)
        pill_x = MARGIN + 6 + w + 4 * mm
        pill_y = PAGE_H - MARGIN - 14
        pill_w = 16 * mm
        pill_h = 6.5 * mm
        filled_rect(c, pill_x, pill_y, pill_w, pill_h,
                    fill=C_ACCENT, r=3)
        txt(c, pill_x + pill_w / 2, pill_y + 1.8 * mm,
            "TODAY", size=7, bold=True, col=C_WHITE, align="center")

    day_bm = f"day_{day_date.isoformat()}"
    draw_nav_buttons(c, "day",
                     month_bm=month_bookmark,
                     week_bm=week_bookmark,
                     day_bm=day_bm)

    # ── Column geometry ────────────────────────────────────────────────────────
    SPLIT    = 0.38                         # time grid fraction of content width
    TIME_W   = CONTENT_W * SPLIT
    PLAN_W   = CONTENT_W - TIME_W
    time_x   = MARGIN
    plan_x   = MARGIN + TIME_W
    grid_top = sep_y - 2 * mm
    grid_bot = MARGIN

    vrule(c, plan_x, grid_bot, grid_top - grid_bot, col=C_SILVER, lw=0.6)

    # ── Time grid (left panel) ────────────────────────────────────────────────
    TIME_LABEL_W = 8 * mm
    grid_x       = time_x + TIME_LABEL_W
    grid_w       = TIME_W - TIME_LABEL_W - 1 * mm

    n_hours = HOUR_END - HOUR_START
    slot_h  = (grid_top - grid_bot) / n_hours

    for i in range(n_hours + 1):
        hour = HOUR_START + i
        y    = grid_top - i * slot_h

        # Hour label (e.g. "7AM")
        if i < n_hours:
            label = f"{hour}{'AM' if hour < 12 else 'PM'}" if hour != 12 else "12PM"
            if hour > 12:
                label = f"{hour - 12}PM"
            txt(c, grid_x - 1.5 * mm, y - 4,
                label, size=9, col=C_GREY, align="right")

        # Hour rule (solid)
        hrule(c, grid_x, y, grid_w, col=C_GHOST, lw=0.5)

        # Half-hour rule (dashed / lighter)
        if i < n_hours:
            c.setStrokeColor(colors.HexColor("#BBBBBB"))
            c.setLineWidth(0.3)
            c.setDash([2, 4])
            c.line(grid_x, y - slot_h / 2, grid_x + grid_w, y - slot_h / 2)
            c.setDash([])

    # Timed event blocks
    timed = sorted([e for e in events if not e.is_all_day], key=lambda e: e.start)
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
        lx = grid_x + li * (grid_w / n_lanes) + 0.5
        lw = grid_w / n_lanes - 1

        for evt in lane:
            ls = evt.start.astimezone(tz)
            le = evt.end.astimezone(tz)
            if ls.hour >= HOUR_END or le.hour < HOUR_START:
                continue

            sf = max(ls.hour + ls.minute / 60, HOUR_START) - HOUR_START
            ef = min(le.hour + le.minute / 60, HOUR_END)   - HOUR_START
            bh = (ef - sf) * slot_h - 1.5
            bt = grid_top - sf * slot_h
            bb = bt - bh

            ec = _event_color(evt.title)
            filled_rect(c, lx, bb, lw, bh, fill=ec, r=2)

            if bh > 7 * mm:
                txt(c, lx + 1.5 * mm, bt - 6 * mm,
                    evt.title, size=8.5, bold=True, col=C_WHITE,
                    max_w=lw - 3 * mm)
            if bh > 14 * mm:
                t_str = f"{ls.strftime('%H:%M')}–{le.strftime('%H:%M')}"
                txt(c, lx + 1.5 * mm, bt - 13 * mm,
                    t_str, size=7, col=C_WHITE, max_w=lw - 3 * mm)

            if evt.id in event_page_map:
                c.linkAbsolute("", f"event_{evt.id}", (lx, bb, lx + lw, bt))

    # ── Planning panel (right) ────────────────────────────────────────────────
    py      = grid_top
    pad     = 3 * mm
    line_h  = 7.5 * mm

    # Quote
    quote, author = _DAY_QUOTES[day_date.toordinal() % len(_DAY_QUOTES)]
    quote_words = quote.split()
    # Wrap into ~2 lines
    qline1, qline2 = "", ""
    for w in quote_words:
        test = (qline1 + " " + w).strip()
        if c.stringWidth(test, "Helvetica-Oblique", 6) < PLAN_W - 2 * pad:
            qline1 = test
        else:
            qline2 = (qline2 + " " + w).strip()

    txt(c, plan_x + pad, py - 6 * mm,
        qline1, size=9, italic=True, col=C_INK_2)
    if qline2:
        txt(c, plan_x + pad, py - 12.5 * mm,
            qline2, size=9, italic=True, col=C_INK_2)
    txt(c, plan_x + pad, py - (19 if qline2 else 15) * mm,
        f"— {author.upper()}", size=7, bold=True, col=C_ACCENT)

    py -= (20 if qline2 else 17) * mm

    # Horizontal rule
    hrule(c, plan_x, py, PLAN_W, col=C_GHOST, lw=0.5)
    py -= 1 * mm

    # FOCUS box (salmon/pink background)
    FOCUS_H = 38 * mm
    focus_fill = colors.HexColor("#FCE4EC")
    filled_rect(c, plan_x + pad, py - FOCUS_H, PLAN_W - 2 * pad, FOCUS_H,
                fill=focus_fill, r=3)

    # "FOCUS" label rotated vertically inside box
    c.saveState()
    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(colors.HexColor("#C06080"))
    c.translate(plan_x + pad + 5 * mm, py - FOCUS_H / 2)
    c.rotate(90)
    c.drawCentredString(0, 0, "FOCUS")
    c.restoreState()

    # Ruled lines inside FOCUS box
    fy = py - 8 * mm
    while fy > py - FOCUS_H + 3 * mm:
        hrule(c, plan_x + pad + 11 * mm, fy,
              PLAN_W - 2 * pad - 12 * mm,
              col=colors.HexColor("#E8B4C0"), lw=0.5)
        fy -= line_h

    py -= FOCUS_H + 4 * mm

    # PRIORITIES
    hrule(c, plan_x, py, PLAN_W, col=C_GHOST, lw=0.5)
    txt(c, plan_x + pad, py - 5 * mm, "PRIORITIES",
        size=9, bold=True, col=C_GREY)
    py -= 12 * mm
    circ_r = 4 * mm
    for n in range(1, 4):
        cx2 = plan_x + pad + circ_r
        if n <= 2:
            circle(c, cx2, py, circ_r, fill=C_ACCENT)
            c.setFont("Helvetica-Bold", 9); c.setFillColor(C_WHITE)
        else:
            circle(c, cx2, py, circ_r, fill=C_GHOST, stroke=C_SILVER, lw=0.5)
            c.setFont("Helvetica-Bold", 9); c.setFillColor(C_SILVER)
        c.drawCentredString(cx2, py - 3, str(n))
        hrule(c, cx2 + circ_r + 2 * mm, py,
              PLAN_W - pad - circ_r * 2 - 7 * mm,
              col=C_GHOST, lw=0.5)
        py -= circ_r * 2 + 3 * mm

    py -= 3 * mm

    # TO DO LIST — bold separator, checkboxes, ruled lines
    hrule(c, plan_x, py, PLAN_W, col=C_INK, lw=1.2)
    txt(c, plan_x + pad, py - 5.5 * mm, "TO DO LIST",
        size=9, bold=True, col=C_GREY)
    py -= 12 * mm
    todo_r = 3.2 * mm
    while py > grid_bot + 32 * mm:
        circle(c, plan_x + pad + todo_r, py, todo_r,
               fill=C_WHITE, stroke=C_SILVER, lw=0.5)
        hrule(c, plan_x + pad + todo_r * 2 + 2 * mm, py,
              PLAN_W - pad - todo_r * 2 - 5 * mm,
              col=C_GHOST, lw=0.5)
        py -= line_h

    # NOTES — bold separator, ruled lines
    hrule(c, plan_x, py, PLAN_W, col=C_INK, lw=1.2)
    txt(c, plan_x + pad, py - 5.5 * mm, "NOTES",
        size=9, bold=True, col=C_GREY)
    py -= 12 * mm
    while py > grid_bot + 2 * mm:
        hrule(c, plan_x + pad, py,
              PLAN_W - 2 * pad, col=C_GHOST, lw=0.5)
        py -= line_h


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
                      week_bookmark: str, tz=None, day_bookmark: str = ""):
    tz  = tz or timezone.utc
    ls  = event.start.astimezone(tz)
    month = ls.month

    draw_tab(c, month, month_bookmark=f"month_{ls.year}_{ls.month:02d}")

    day_label = ls.strftime("%A %-d %B %Y")

    sep_y = draw_page_header(
        c,
        left_label=event.title,
        left_sub=day_label,
        accent_bar=True,
    )

    month_bm = f"month_{ls.year}_{ls.month:02d}"
    day_bm   = day_bookmark or f"day_{ls.date().isoformat()}"
    draw_nav_buttons(c, "day", month_bm=month_bm, week_bm=week_bookmark, day_bm=day_bm)

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
        hrule(c, MARGIN, y, CONTENT_W, col=C_GHOST, lw=0.6)
        y -= 5 * mm
        txt(c, MARGIN, y, "Attendees", size=9, bold=True, col=C_INK_2)
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
        hrule(c, MARGIN, y, CONTENT_W, col=C_GHOST, lw=0.6)
        y -= 5 * mm
        txt(c, MARGIN, y, "Agenda", size=9, bold=True, col=C_INK_2)
        y -= 7 * mm
        words = event.description.split()
        line  = ""
        for word in words:
            test = (line + " " + word).strip()
            if c.stringWidth(test, "Helvetica", 8.5) < CONTENT_W:
                line = test
            else:
                if y < MARGIN + 40 * mm: break
                txt(c, MARGIN, y, line, size=8.5, col=C_INK_2)
                y -= 6 * mm; line = word
        if line and y > MARGIN + 40 * mm:
            txt(c, MARGIN, y, line, size=8.5, col=C_INK_2)
            y -= 9 * mm

    # ── Notes section ─────────────────────────────────────────────────────────
    hrule(c, MARGIN, y, CONTENT_W, col=C_SILVER, lw=0.6)
    y -= 5 * mm
    txt(c, MARGIN, y, "Notes", size=9, bold=True, col=C_INK_2)

    # Accent bar to the left of the writing area
    filled_rect(c, MARGIN, MARGIN, 2, y - 4 * mm - MARGIN,
                fill=colors.HexColor(MONTH_TAB_COLORS[(event.start.astimezone(tz).month - 1)]))

    y -= 9 * mm
    while y > MARGIN + 6 * mm:
        hrule(c, MARGIN + 4 * mm, y, CONTENT_W - 4 * mm,
              col=C_GHOST, lw=0.5)
        y -= 9 * mm


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

    # day_day_map: "YYYY-MM-DD" → day bookmark key
    day_day_map: dict[str, str] = {}
    for w in weeks:
        for i in range(5):
            d = w + timedelta(days=i)
            dk = d.strftime("%Y-%m-%d")
            day_day_map[dk] = f"day_{d.isoformat()}"

    # ── Pass 1: build event lookup tables ────────────────────────────────────
    # Timed events that fall on a weekday covered by a week page
    timed_events = [
        e for e in events
        if not e.is_all_day
        and e.start.astimezone(tz).strftime("%Y-%m-%d") in day_week_map
    ]
    timed_events.sort(key=lambda e: e.start)

    event_pg:    dict[str, int] = {}   # id → truthy (page number unused; existence check only)
    event_wk_bm: dict[str, str] = {}
    event_day_bm: dict[str, str] = {}
    for i, e in enumerate(timed_events, start=1):
        event_pg[e.id]     = i
        dk                 = e.start.astimezone(tz).strftime("%Y-%m-%d")
        event_wk_bm[e.id]  = day_week_map[dk]
        event_day_bm[e.id] = day_day_map[dk]

    # Group weeks under the active month they first overlap with (Mon's month,
    # or next active month if that Monday falls before the range).
    active_month_set = set(months)

    def _week_month_key(w):
        for i in range(7):
            key = ((w + timedelta(days=i)).year, (w + timedelta(days=i)).month)
            if key in active_month_set:
                return key
        return (w.year, w.month)

    from collections import defaultdict
    weeks_by_month: dict = defaultdict(list)
    for w in weeks:
        weeks_by_month[_week_month_key(w)].append(w)

    # Group timed events under their week Monday
    events_by_week: dict = defaultdict(list)
    for e in timed_events:
        dk   = e.start.astimezone(tz).strftime("%Y-%m-%d")
        wbm  = day_week_map[dk]
        wmon = date.fromisoformat(wbm.replace("week_", ""))
        events_by_week[wmon].append(e)

    # ── Pass 2: draw pages in interleaved order ───────────────────────────────
    # Order: Year → [Month overview → Week → Event notes → Week → …] × months
    c = canvas.Canvas(output_path, pagesize=A4)
    c.setTitle(title)
    c.setAuthor("PolarisFolio")

    # Year overview (first page)
    year_val = start_date.year
    c.bookmarkPage(YEAR_BM)
    c.addOutlineEntry(str(year_val), YEAR_BM, level=0)
    active_months_set_int = {month for (yr, month) in months if yr == year_val}
    draw_year_page(c, year_val, day_week_map, active_months=active_months_set_int, tz=tz)
    c.showPage()

    for year, month in months:
        # Month overview
        month_bm = f"month_{year}_{month:02d}"
        c.bookmarkPage(month_bm)
        c.addOutlineEntry(
            datetime(year, month, 1).strftime("%B %Y"), month_bm, level=0)
        draw_month_page(c, year, month, ev_by_month[(year, month)], day_week_map, tz=tz)
        c.showPage()

        # Weeks that belong to this month, in chronological order
        for w in weeks_by_month.get((year, month), []):
            week_bm = f"week_{w.isoformat()}"
            c.bookmarkPage(week_bm)
            fri = w + timedelta(days=4)
            c.addOutlineEntry(
                f"Week {w.isocalendar()[1]} · {w.strftime('%-d %b')} – {fri.strftime('%-d %b')}",
                week_bm, level=1)
            wk_evts = {
                (w + timedelta(days=i)).strftime("%Y-%m-%d"):
                ev_by_day.get((w + timedelta(days=i)).strftime("%Y-%m-%d"), [])
                for i in range(5)
            }
            # First day page for this week → day button target on week page
            first_day_bm_week = f"day_{w.isoformat()}"
            draw_week_page(c, w, wk_evts, event_pg, tz, month_bookmark=month_bm)
            c.showPage()

            # Day pages (Mon–Fri)
            events_by_day_in_week: dict = defaultdict(list)
            for e in events_by_week.get(w, []):
                dk = e.start.astimezone(tz).strftime("%Y-%m-%d")
                events_by_day_in_week[dk].append(e)

            for i in range(5):
                day_d = w + timedelta(days=i)
                d_bm  = f"day_{day_d.isoformat()}"
                c.bookmarkPage(d_bm)
                c.addOutlineEntry(
                    f"  {day_d.strftime('%a %-d %b')}", d_bm, level=2)
                day_ev = ev_by_day.get(day_d.strftime("%Y-%m-%d"), [])
                draw_day_page(c, day_d, day_ev, event_pg, tz,
                              week_bookmark=week_bm,
                              month_bookmark=month_bm)
                c.showPage()

            # Event detail pages for this week, sorted by start time
            for e in sorted(events_by_week.get(w, []), key=lambda e: e.start):
                event_bm = f"event_{e.id}"
                c.bookmarkPage(event_bm)
                c.addOutlineEntry(f"    {e.title[:40]}", event_bm, level=3)
                draw_meeting_page(c, e, event_wk_bm[e.id], tz=tz,
                                  day_bookmark=event_day_bm.get(e.id, ""))
                c.showPage()

    c.save()
    print(f"PDF saved: {output_path}")
    return output_path


# ─────────────────────────────────────────────────────────────────────────────
# Quick self-test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from models import CalendarEvent, Attendee

    today = datetime.now(tz).date()

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
