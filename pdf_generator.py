"""
PDF generator for the PolarisFolio planner — Dayfolio-style weekly spread.

Structure per generated PDF:
  • Monthly overview pages  (week-number grid, event bars, tap → weekly page)
  • Weekly spread pages     (Mon–Fri columns, timed event blocks, bottom sections)
  • Meeting note pages      (metadata + ruled writing area)

Optimised for reMarkable Paper Pro (A4 portrait, colour e-ink display).
"""

import os
import re
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

# Per-month tab palette (Jan→Dec) — used on every page's right-edge tab
MONTH_TAB_COLORS = [
    "#5BBFBF", "#6FB0E0", "#9B9BD4", "#8E96D6", "#E2A0C0", "#E58B8B",
    "#E59A7A", "#E0B870", "#C9B98A", "#C99A7A", "#9AA8C0", "#8FB0A0",
]
C_TODAY_NAVY = colors.HexColor("#1F3A5F")   # overview today circle
C_CUR_PILL   = colors.HexColor("#E0656F")   # current-month pill


def _event_color(title: str) -> colors.Color:
    """Deterministic pastel colour from event title."""
    return EVENT_PALETTE[hash(title) % len(EVENT_PALETTE)]


def _week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _fmt(dt, fmt: str) -> str:
    """strftime with a portable %-d (no leading zero) on Windows + POSIX."""
    return dt.strftime(fmt.replace("%-d", str(dt.day)))


def _sunday_week_of_year(d: date) -> int:
    """dayfo.io-style week number: Sunday-based weeks counted from 1 Jan
    (Jan rows 1-5, Feb 6-9, …). A trailing week that rolls into the next
    year is shown as that year's week 1."""
    jan1_sun_idx = (date(d.year, 1, 1).weekday() + 1) % 7   # Sun=0..Sat=6
    wk  = (d.timetuple().tm_yday + jan1_sun_idx - 1) // 7 + 1
    sat = d + timedelta(days=(6 - (d.weekday() + 1) % 7))   # Sat of d's week
    return 1 if sat.year > d.year else wk


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


def hatch_rect(c: canvas.Canvas, x, y, w, h, col=None, gap=2.4, lw=0.3):
    """Fill a rectangle with 45° diagonal hatching (clipped to bounds).
    Used to grey-out days that fall outside the displayed month."""
    c.saveState()
    p = c.beginPath()
    p.rect(x, y, w, h)
    c.clipPath(p, stroke=0, fill=0)
    c.setStrokeColor(col if col is not None else C_GHOST)
    c.setLineWidth(lw)
    step = gap * mm
    i = -int(h / step) - 1
    x_end = x + w
    while x + i * step <= x_end:
        x0 = x + i * step
        c.line(x0, y, x0 + h, y + h)
        i += 1
    c.restoreState()


# ─────────────────────────────────────────────────────────────────────────────
# Navigation buttons (top-right of every page header)
# ─────────────────────────────────────────────────────────────────────────────

YEAR_BM = "year_overview"   # bookmark for the year overview page
MEETINGS_BM = "meetings"    # bookmark for the meetings agenda page

C_WHITE = colors.white      # convenience alias used in nav icon drawing


def _draw_grid_icon(c: canvas.Canvas, cx, cy, sz, col):
    """3×3 grid of tiny squares — the year overview symbol."""
    dot = sz * 0.13
    step = sz * 0.33
    c.setFillColor(col)
    for row in range(3):
        for ci in range(3):
            dx = cx + (ci - 1) * step
            dy = cy + (row - 1) * step
            c.rect(dx - dot / 2, dy - dot / 2, dot, dot, fill=1, stroke=0)


def _draw_list_icon(c: canvas.Canvas, cx, cy, sz, col):
    """Agenda/list symbol — three rows, each a leading dot + line."""
    row_gap = sz * 0.34
    line_w  = sz * 0.52
    dot_r   = sz * 0.075
    x0 = cx - sz * 0.34
    for r in range(3):
        ly = cy + (r - 1) * row_gap
        c.setFillColor(col)
        c.circle(x0, ly, dot_r, fill=1, stroke=0)
        c.setStrokeColor(col); c.setLineWidth(0.8)
        c.line(x0 + dot_r + 1.0 * mm, ly, x0 + dot_r + 1.0 * mm + line_w, ly)


def _draw_cal_button(c: canvas.Canvas, cx, cy, bw, bh, label, col):
    """Tear-off calendar glyph (two rings on top) with a centred label."""
    x = cx - bw / 2
    y = cy - bh / 2
    hb = bh * 0.24                        # 'binding' strip height
    # Rings poking above the top edge
    c.setStrokeColor(col); c.setLineWidth(0.8)
    for fx in (0.34, 0.66):
        rx = x + bw * fx
        c.line(rx, y + bh - 0.2 * mm, rx, y + bh + 1.0 * mm)
    # Body
    c.setFillColor(C_WHITE); c.setStrokeColor(col); c.setLineWidth(0.7)
    c.roundRect(x, y, bw, bh, 1.2, fill=1, stroke=1)
    # Binding rule near the top
    hrule(c, x + 1.0 * mm, y + bh - hb, bw - 2.0 * mm, col=col, lw=0.6)
    # Label centred in the lower area, shrunk to fit if needed
    fs = 7.0
    while fs > 4.5 and c.stringWidth(label, "Helvetica-Bold", fs) > bw - 2.2 * mm:
        fs -= 0.5
    area_mid = y + (bh - hb) / 2
    txt(c, cx, area_mid - fs * 0.34, label, size=fs, bold=True, col=col, align="center")


# Nav context set by build_planner before drawing pages (so the calendar
# buttons can show "today" and only link to pages that actually exist).
_NAV_TODAY = None              # date
_NAV_VALID_BMS: set = set()    # bookmark names that exist in this PDF


def draw_nav_buttons(c: canvas.Canvas, active: str, omit_year: bool = False,
                     **_legacy):
    """
    Top-right navigation: Year (grid) · Month · Week · Day · List.
    Month/Week/Day are labelled tear-off calendar buttons showing TODAY
    (e.g. JUN / W24 / 13) that jump to today's pages when those pages exist.
    The List button jumps to the meetings agenda page.
    `active` highlights the current page type in the accent colour.
    `omit_year` drops the leading grid button (used on the year page itself).
    """
    today = _NAV_TODAY or date.today()
    valid = _NAV_VALID_BMS
    wk    = _sunday_week_of_year(today)
    mon   = _week_monday(today)

    buttons = [
        # name,    kind,   label,                          bookmark
        ("year",  "grid", "",                              YEAR_BM),
        ("month", "cal",  today.strftime("%b").upper(),    f"month_{today.year}_{today.month:02d}"),
        ("week",  "cal",  f"W{wk}",                        f"week_{mon.isoformat()}"),
        ("day",   "cal",  str(today.day),                  f"day_{today.isoformat()}"),
        ("list",  "list", "",                              MEETINGS_BM),
    ]
    if omit_year:
        buttons = [b for b in buttons if b[0] != "year"]

    BH    = 6.5 * mm
    BW_C  = 9.0 * mm      # labelled calendar button
    BW_I  = 7.0 * mm      # icon-only button (year, list)
    GAP   = 1.8 * mm
    right = MARGIN + CONTENT_W
    top   = PAGE_H - MARGIN
    cy    = top - 13      # vertically centred in the header band

    widths  = [BW_I if k in ("grid", "list") else BW_C for (_, k, _, _) in buttons]
    total_w = sum(widths) + GAP * (len(buttons) - 1)
    x = right - total_w

    for (name, kind, label, bm), bw in zip(buttons, widths):
        cx  = x + bw / 2
        col = C_ACCENT if name == active else C_GREY
        if kind == "grid":
            _draw_grid_icon(c, cx, cy, BH * 0.9, col)
        elif kind == "list":
            _draw_list_icon(c, cx, cy, BH * 0.9, col)
        else:
            _draw_cal_button(c, cx, cy, bw, BH, label, col)
        if bm and bm in valid:
            c.linkAbsolute("", bm, (cx - bw / 2, cy - BH / 2, cx + bw / 2, cy + BH / 2))
        x += bw + GAP


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


def _draw_year_edge_tabs(c: canvas.Canvas, year: int, active_months: set = None,
                         active_month: int = None):
    """Right-edge tab stack: a "year" cap + 12 labelled month segments.
    Each month segment links to that month's overview page (when it exists).
    `active_month` renders that segment as a white "current" tab with coloured
    text (used on the month overview pages).
    Shared by the year overview, month overview and meetings agenda pages."""
    tab_x = PAGE_W - TAB_W
    n_seg = 13                       # 1 year cap + 12 months
    seg_h = PAGE_H / n_seg

    def _tab_label(seg_y0: float, text: str, col, text_col=C_WHITE):
        filled_rect(c, tab_x, seg_y0, TAB_W, seg_h, fill=col)
        c.saveState()
        c.setFillColor(text_col)
        c.setFont("Helvetica-Bold", 7)
        c.translate(tab_x + TAB_W / 2, seg_y0 + seg_h / 2)
        c.rotate(90)
        c.drawCentredString(0, -2.3, text)
        c.restoreState()

    # Year cap (top segment)
    _tab_label(PAGE_H - seg_h, str(year), C_INK)

    # Month segments, each its own pastel + rotated abbreviation
    for m in range(1, 13):
        y0 = PAGE_H - (m + 1) * seg_h
        m_col = colors.HexColor(MONTH_TAB_COLORS[(m - 1) % 12])
        if m == active_month:
            # Highlighted current month: white segment, coloured label
            _tab_label(y0, datetime(year, m, 1).strftime("%b").upper(),
                       C_WHITE, text_col=m_col)
        else:
            _tab_label(y0, datetime(year, m, 1).strftime("%b").upper(), m_col)
        if active_months is None or m in active_months:
            mbm = f"month_{year}_{m:02d}"
            c.linkAbsolute("", mbm, (tab_x, y0, PAGE_W, y0 + seg_h))
    vrule(c, tab_x, 0, PAGE_H, col=colors.HexColor("#00000018"), lw=0.5)


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

    # ── Right-edge tabs: "year" cap + 12 labeled month segments ───────────────
    _draw_year_edge_tabs(c, year, active_months=active_months)

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

    # Year page shows only Month · Week · Day · List (no leading grid button)
    draw_nav_buttons(c, "year", omit_year=True)

    # ── Mini-month grid ───────────────────────────────────────────────────────
    COL_GAP  = 5 * mm
    ROW_GAP  = 4 * mm
    avail_w  = CONTENT_W
    avail_h  = sep_y - MARGIN
    cell_w   = (avail_w - 2 * COL_GAP) / 3
    cell_h   = (avail_h - 3 * ROW_GAP) / 4

    # Vertical rhythm inside a cell (block is centred within the cell)
    NAME_OFFS = 4.5 * mm   # content top → month-name baseline
    DOW_OFFS  = 5.2 * mm   # name → DOW row
    GRID_OFFS = 3.4 * mm   # DOW row → first date row top
    ROW_H     = 6.0 * mm   # date row height
    N_ROWS    = 6          # max weeks shown (uniform across months)
    block_h   = NAME_OFFS + DOW_OFFS + GRID_OFFS + N_ROWS * ROW_H

    for m in range(1, 13):
        row  = (m - 1) // 3
        col  = (m - 1) % 3
        cx0  = MARGIN + col * (cell_w + COL_GAP)
        cell_top = sep_y - ROW_GAP - row * (cell_h + ROW_GAP)
        content_top = cell_top - max(0, (cell_h - block_h) / 2)

        is_cur = (m == today.month and year == today.year)

        # Week gutter + 7 day columns
        wk_w   = cell_w * 0.11
        day_w  = (cell_w - wk_w) / 7
        day_x0 = cx0 + wk_w

        # Month name (coral pill if current month)
        mname  = datetime(year, m, 1).strftime("%B").upper()
        name_y = content_top - NAME_OFFS
        if is_cur:
            pw = c.stringWidth(mname, "Helvetica-Bold", 7) + 4 * mm
            pill_x = cx0 + cell_w / 2 - pw / 2
            filled_rect(c, pill_x, name_y - 1.6 * mm, pw, 5.4 * mm,
                        fill=C_CUR_PILL, r=2)
            txt(c, cx0 + cell_w / 2, name_y, mname,
                size=7, bold=True, col=C_WHITE, align="center")
        else:
            txt(c, cx0 + cell_w / 2, name_y, mname,
                size=7, bold=True, col=C_INK, align="center")

        # Tap the month name → that month's overview page (when it exists)
        if active_months is None or m in active_months:
            c.linkAbsolute("", f"month_{year}_{m:02d}",
                           (cx0, name_y - 2 * mm, cx0 + cell_w, name_y + 4 * mm))

        # DOW row:  W  S M T W T F S
        dow_y = name_y - DOW_OFFS
        txt(c, cx0 + wk_w / 2, dow_y, "W", size=5.5, col=C_SILVER, align="center")
        for di, d in enumerate(_MINI_DOW):
            dx = day_x0 + di * day_w + day_w / 2
            col_d = C_GREY if di in (0, 6) else C_INK_2
            txt(c, dx, dow_y, d, size=6, col=col_d, align="center")
        hrule(c, cx0, dow_y - 1 * mm, cell_w, col=C_GHOST, lw=0.5)

        # Date grid
        weeks    = calendar.Calendar(firstweekday=6).monthdayscalendar(year, m)
        grid_top = dow_y - GRID_OFFS
        grid_bot = grid_top - N_ROWS * ROW_H

        # Weekend column shading (full grid height)
        for di in (0, 6):
            sx = day_x0 + di * day_w
            filled_rect(c, sx, grid_bot, day_w, grid_top - grid_bot, fill=C_WKND)

        for r, wk in enumerate(weeks):
            ry = grid_top - r * ROW_H - ROW_H / 2   # row centre
            dy = ry - 2                              # text baseline

            # Week number in the gutter
            sample = next((d for d in wk if d), None)
            if sample:
                wk_num = _sunday_week_of_year(date(year, m, sample))
                txt(c, cx0 + wk_w / 2, dy, str(wk_num),
                    size=5.5, col=C_SILVER, align="center")

            for di, day_num in enumerate(wk):
                if day_num == 0:
                    continue
                dx = day_x0 + di * day_w + day_w / 2
                is_td   = (day_num == today.day and m == today.month
                           and year == today.year)
                is_wknd = di in (0, 6)

                if is_td:
                    circle(c, dx, ry, 2.6 * mm, fill=C_TODAY_NAVY)
                    txt(c, dx, dy, str(day_num), size=6, bold=True,
                        col=C_WHITE, align="center")
                else:
                    num_col = C_GREY if is_wknd else C_INK_2
                    txt(c, dx, dy, str(day_num), size=6,
                        col=num_col, align="center")

                # Tap → week page
                day_key = date(year, m, day_num).strftime("%Y-%m-%d")
                if day_key in day_week_map:
                    c.linkAbsolute("", day_week_map[day_key],
                                   (dx - day_w / 2, ry - ROW_H / 2,
                                    dx + day_w / 2, ry + ROW_H / 2))


# ─────────────────────────────────────────────────────────────────────────────
# Meetings agenda page
# ─────────────────────────────────────────────────────────────────────────────

def _draw_person_glyph(c: canvas.Canvas, cx, cy, sz, col):
    """Tiny attendee/person symbol — head dot above rounded shoulders."""
    c.setFillColor(col)
    c.circle(cx, cy + sz * 0.30, sz * 0.26, fill=1, stroke=0)
    bw, bh = sz * 0.78, sz * 0.40
    c.roundRect(cx - bw / 2, cy - sz * 0.42, bw, bh, bh / 2, fill=1, stroke=0)


def draw_meetings_page(c: canvas.Canvas, year: int, events: list,
                       event_page_map: dict,
                       active_months: set = None, tz=timezone.utc):
    """
    Agenda-style list of all meetings, grouped by month with a coloured month
    header. Events flow down the left column then the right column. Each row
    taps through to that meeting's note page. Reachable from the List nav
    button on every page.
    """
    # ── Right-edge tabs (same stack as the year page) ─────────────────────────
    _draw_year_edge_tabs(c, year, active_months=active_months)

    # ── Header: list glyph + "MEETINGS" ───────────────────────────────────────
    top = PAGE_H - MARGIN
    _draw_list_icon(c, MARGIN + 3 * mm, top - 8, 6.5 * mm, C_INK)
    c.setFont("Helvetica-Bold", 15)
    c.setFillColor(C_INK)
    c.drawString(MARGIN + 9 * mm, top - 11, "MEETINGS")

    sep_y = top - 26
    hrule(c, MARGIN, sep_y, CONTENT_W, col=C_SILVER, lw=0.6)

    draw_nav_buttons(c, "list")

    # ── Two-column agenda layout ──────────────────────────────────────────────
    COL_GAP = 6 * mm
    col_w   = (CONTENT_W - COL_GAP) / 2
    col_x   = [MARGIN, MARGIN + col_w + COL_GAP]
    col_top = sep_y - 6 * mm
    col_bot = MARGIN
    vrule(c, MARGIN + col_w + COL_GAP / 2, col_bot, col_top - col_bot,
          col=C_GHOST, lw=0.5)

    meetings = sorted([e for e in events if not e.is_all_day],
                      key=lambda e: e.start)

    if not meetings:
        txt(c, MARGIN, col_top - 6 * mm, "No meetings scheduled.",
            size=9, italic=True, col=C_GREY)
        return

    # Group by (year, month) preserving chronological order
    from collections import OrderedDict
    groups: "OrderedDict[tuple, list]" = OrderedDict()
    for e in meetings:
        loc = e.start.astimezone(tz)
        groups.setdefault((loc.year, loc.month), []).append(e)

    HDR_H = 7 * mm
    ROW_H = 9 * mm

    ci = 0           # current column index
    y  = col_top

    def _advance_if_needed(h: float) -> bool:
        """Ensure `h` of vertical room; spill to the next column if not.
        Returns False once we run out of columns."""
        nonlocal ci, y
        if y - h < col_bot:
            ci += 1
            y = col_top
        return ci < 2

    for (yr, mo), evs in groups.items():
        # Keep a month header with at least its first row (avoid orphan header)
        if not _advance_if_needed(HDR_H + ROW_H):
            break
        x = col_x[ci]
        mname = datetime(yr, mo, 1).strftime("%B %Y").upper()
        pill_col = colors.HexColor(MONTH_TAB_COLORS[(mo - 1) % 12])
        filled_rect(c, x, y - HDR_H + 1 * mm, col_w, HDR_H - 1 * mm,
                    fill=pill_col, r=2)
        circle(c, x + 3 * mm, y - HDR_H / 2 + 0.5 * mm, 1.2 * mm, fill=C_WHITE)
        txt(c, x + 6 * mm, y - HDR_H + 3.2 * mm, mname,
            size=8, bold=True, col=C_WHITE)
        y -= HDR_H + 2 * mm

        for e in evs:
            if not _advance_if_needed(ROW_H):
                break
            x   = col_x[ci]
            loc = e.start.astimezone(tz)

            # Accent stripe on the left of the row
            filled_rect(c, x, y - ROW_H + 1.5 * mm, 1.5, ROW_H - 2 * mm,
                        fill=_event_color(e.title))
            # Day number + weekday
            txt(c, x + 3 * mm, y - 3.8 * mm, str(loc.day),
                size=10, bold=True, col=C_INK)
            txt(c, x + 3 * mm, y - 7.2 * mm, loc.strftime("%a").upper(),
                size=6, col=C_GREY)
            # Time · duration
            txt(c, x + 11 * mm, y - 7 * mm,
                f"{loc.strftime('%H:%M')} · {e.duration_str}",
                size=6, col=C_GREY)
            # Title
            txt(c, x + 11 * mm, y - 3.8 * mm, e.title, size=9, bold=True,
                col=C_INK, max_w=col_w - 26 * mm)
            # Attendee count
            if e.attendees:
                cnt = str(len(e.attendees))
                _draw_person_glyph(c, x + col_w - 7 * mm, y - 4.5 * mm,
                                   3 * mm, C_GREY)
                txt(c, x + col_w - 4.5 * mm, y - 5.5 * mm, cnt,
                    size=7, col=C_GREY)
            # Tap → meeting note page (when one exists for this event)
            if e.id in event_page_map:
                c.linkAbsolute("", f"event_{e.id}",
                               (x, y - ROW_H, x + col_w, y))
            # Row divider
            hrule(c, x + 3 * mm, y - ROW_H + 0.5 * mm, col_w - 3 * mm,
                  col=C_GHOST, lw=0.3)
            y -= ROW_H
        y -= 2 * mm


# ─────────────────────────────────────────────────────────────────────────────
# Monthly overview page
# ─────────────────────────────────────────────────────────────────────────────

_DOW = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]
_DOW_FULL = ["SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY",
             "THURSDAY", "FRIDAY", "SATURDAY"]

def draw_month_page(c: canvas.Canvas, year: int, month: int,
                    events: list,
                    day_week_map: dict,
                    tz=timezone.utc,
                    active_months: set = None):
    """
    Full-page monthly calendar grid (Dayfolio style).

    • Right-edge year tab stack with the current month highlighted
    • Header: month + year, centred "CALENDAR" label
    • Columns: week-num gutter + SUNDAY … SATURDAY (full names)
    • Tall cells: day number top-right, leading/trailing days hatched, event
      pills below the number
    • Ruled notes area along the bottom
    """
    today      = date.today()
    month_name = datetime(year, month, 1).strftime("%B").upper()

    # ── Right-edge tab stack (current month highlighted) ──────────────────────
    _draw_year_edge_tabs(c, year, active_months=active_months, active_month=month)

    # ── Header: "MONTH year" + centred CALENDAR label ─────────────────────────
    top = PAGE_H - MARGIN
    c.setFont("Helvetica-Bold", 15)
    c.setFillColor(C_INK)
    c.drawString(MARGIN, top - 11, month_name)
    mw = c.stringWidth(month_name, "Helvetica-Bold", 15)
    txt(c, MARGIN + mw + 5, top - 11, str(year), size=15, col=C_GREY)

    month_bm = f"month_{year}_{month:02d}"
    week_bm  = day_week_map.get(date(year, month, 1).strftime("%Y-%m-%d"), "")
    draw_nav_buttons(c, "month", month_bm=month_bm, week_bm=week_bm)

    cal_label = "CALENDAR"
    cs = 1.5   # extra letter spacing
    cal_w = c.stringWidth(cal_label, "Helvetica-Bold", 7) + cs * len(cal_label)
    to = c.beginText()
    to.setFont("Helvetica-Bold", 7)
    to.setFillColor(C_GREY)
    to.setCharSpace(cs)
    to.setTextOrigin(MARGIN + CONTENT_W / 2 - cal_w / 2, top - 22)
    to.textOut(cal_label)
    c.drawText(to)

    sep_y = top - 28
    hrule(c, MARGIN, sep_y, CONTENT_W, col=C_SILVER, lw=0.6)

    # ── Events grouped by in-month day number ─────────────────────────────────
    ev_by_day: dict[int, list] = {}
    for e in events:
        loc = e.start.astimezone(tz)
        if loc.year == year and loc.month == month:
            ev_by_day.setdefault(loc.day, []).append(e)

    cal_weeks = calendar.Calendar(firstweekday=6).monthdatescalendar(year, month)
    n_weeks   = len(cal_weeks)

    # ── Geometry ──────────────────────────────────────────────────────────────
    WEEK_COL_W = 8 * mm                       # week-number gutter
    WKDAY_W    = (CONTENT_W - WEEK_COL_W) / 7
    DOW_ROW_H  = 7 * mm                        # weekday-name header row
    NOTES_H    = 26 * mm                       # ruled notes area at the bottom

    grid_top = sep_y - 3 * mm
    grid_bot = MARGIN + NOTES_H
    dow_y    = grid_top - DOW_ROW_H           # top of the cell body
    body_h   = dow_y - grid_bot
    cell_h   = body_h / n_weeks

    grid_left  = MARGIN
    grid_right = MARGIN + CONTENT_W
    day_left   = MARGIN + WEEK_COL_W

    # ── Weekday header row (full names) ───────────────────────────────────────
    for col, label in enumerate(_DOW_FULL):
        cx = day_left + col * WKDAY_W
        is_wknd = col in (0, 6)
        txt(c, cx + WKDAY_W / 2, dow_y + 2 * mm, label,
            size=7, bold=True,
            col=C_GREY if is_wknd else C_INK_2, align="center")

    # ── Weekend column shading (body height) ──────────────────────────────────
    for col in (0, 6):
        filled_rect(c, day_left + col * WKDAY_W, grid_bot,
                    WKDAY_W, body_h, fill=C_WKND)

    # ── Cell backgrounds (hatch out-of-month days) + content ──────────────────
    for row, week in enumerate(cal_weeks):
        row_y_top = dow_y - row * cell_h
        row_y_bot = row_y_top - cell_h

        # Week number, centred in the gutter, linked to the week page
        wk_num = _sunday_week_of_year(week[0])
        txt(c, MARGIN + WEEK_COL_W / 2, row_y_top - cell_h / 2 - 2,
            f"W{wk_num}", size=7, col=C_SILVER, align="center")
        row_wbm = next((day_week_map[d.isoformat()] for d in week
                        if d.isoformat() in day_week_map), "")
        if row_wbm:
            c.linkAbsolute("", row_wbm,
                           (grid_left, row_y_bot, day_left, row_y_top))

        for col, d in enumerate(week):
            cx          = day_left + col * WKDAY_W
            in_month    = (d.month == month and d.year == year)
            is_today    = (d == today)

            if not in_month:
                # Adjacent-month day: hatch the cell, grey number, no events
                hatch_rect(c, cx, row_y_bot, WKDAY_W, cell_h,
                           col=C_SILVER, gap=2.6)
                txt(c, cx + WKDAY_W - 2.5 * mm, row_y_top - 5 * mm,
                    str(d.day), size=8, col=C_SILVER, align="right")
                continue

            # ── Day number (top-right) ─────────────────────────────
            num_x = cx + WKDAY_W - 2.5 * mm
            num_y = row_y_top - 5 * mm
            if is_today:
                circle(c, cx + WKDAY_W - 4 * mm, num_y + 1.3 * mm,
                       4 * mm, fill=C_ACCENT)
                txt(c, num_x, num_y, str(d.day),
                    size=8, bold=True, col=C_WHITE, align="right")
            else:
                txt(c, num_x, num_y, str(d.day),
                    size=8, bold=False, col=C_INK, align="right")

            # ── Event pills (below the number) ─────────────────────
            day_evts = ev_by_day.get(d.day, [])
            pill_x   = cx + 1 * mm
            pill_w   = WKDAY_W - 2 * mm
            pill_h   = 5.5 * mm
            pill_gap = 1.2 * mm
            pill_y   = row_y_top - 9 * mm
            for evt in day_evts[:3]:
                if pill_y - pill_h < row_y_bot + 1 * mm:
                    circle(c, cx + 3 * mm, row_y_bot + 3 * mm,
                           1.5 * mm, fill=C_SILVER)
                    break
                ec = _event_color(evt.title)
                filled_rect(c, pill_x, pill_y - pill_h,
                            pill_w, pill_h, fill=ec, r=2)
                txt(c, pill_x + 2 * mm, pill_y - pill_h + 1.8 * mm,
                    evt.title, size=8, bold=True, col=C_WHITE,
                    max_w=pill_w - 3 * mm)
                pill_y -= pill_h + pill_gap

            # Tap target → weekly page
            dk = d.isoformat()
            if dk in day_week_map:
                c.linkAbsolute("", day_week_map[dk],
                               (cx, row_y_bot, cx + WKDAY_W, row_y_top))

    # ── Grid lines (on top of cell content) ───────────────────────────────────
    for r in range(n_weeks + 1):
        y = dow_y - r * cell_h
        hrule(c, grid_left, y, CONTENT_W, col=C_GHOST, lw=0.5)
    for col in range(8):
        vx = day_left + col * WKDAY_W
        vrule(c, vx, grid_bot, body_h, col=C_GHOST, lw=0.5)
    vrule(c, grid_left, grid_bot, body_h, col=C_GHOST, lw=0.5)   # outer-left

    # ── Notes area (ruled lines along the bottom) ─────────────────────────────
    ny = grid_bot - 6 * mm
    while ny > MARGIN - 1:
        hrule(c, grid_left, ny, CONTENT_W, col=C_GHOST, lw=0.5)
        ny -= 6.5 * mm


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
    week_num    = _sunday_week_of_year(week_monday)

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
    day_label = _fmt(day_date, "%A, %B %-d").upper()
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


# A horizontal rule of underscores — Microsoft brackets the Teams join block
# (URL, Meeting ID, passcode, dial-in, "Join the meeting now", etc.) between two
# of these in the event body / bodyPreview.
_RULE_RE = re.compile(r"_{4,}")
# Markers that identify an underscore-delimited block as Teams boilerplate.
_TEAMS_MARKERS = (
    "microsoft teams",
    "teams meeting",
    "join the meeting",
    "join on your computer",
    "click here to join",
    "meeting id:",
    "meeting options",
)
_TEAMS_URL_RE = re.compile(r"https?://teams\.(?:microsoft|live)\.com\S*", re.I)


def _strip_teams_boilerplate(text: str) -> str:
    """Remove the Microsoft Teams join blurb from a meeting description so only
    the real agenda remains. Drops any underscore-delimited section containing
    Teams markers, then sweeps up any stray Teams join URLs."""
    if not text:
        return ""
    parts = _RULE_RE.split(text)
    if len(parts) > 1:
        kept = [p for p in parts
                if not any(m in p.lower() for m in _TEAMS_MARKERS)]
        text = " ".join(kept)
    text = _TEAMS_URL_RE.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def draw_meeting_page(c: canvas.Canvas, event: CalendarEvent,
                      week_bookmark: str, tz=None, day_bookmark: str = ""):
    tz  = tz or timezone.utc
    ls  = event.start.astimezone(tz)
    month = ls.month

    draw_tab(c, month, month_bookmark=f"month_{ls.year}_{ls.month:02d}")

    day_label = _fmt(ls, "%A %-d %B %Y")

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
    agenda = _strip_teams_boilerplate(event.description)
    if agenda:
        hrule(c, MARGIN, y, CONTENT_W, col=C_GHOST, lw=0.6)
        y -= 5 * mm
        txt(c, MARGIN, y, "Agenda", size=9, bold=True, col=C_INK_2)
        y -= 7 * mm
        words = agenda.split()
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

    # Nav context: today + the set of bookmarks that actually exist, so the
    # top "jump to today" calendar buttons only link to real pages.
    global _NAV_TODAY, _NAV_VALID_BMS
    _NAV_TODAY = datetime.now(tz).date()
    _NAV_VALID_BMS = {YEAR_BM, MEETINGS_BM}
    _NAV_VALID_BMS |= {f"month_{y}_{m:02d}" for (y, m) in months}
    _NAV_VALID_BMS |= {f"week_{wm.isoformat()}" for wm in weeks}
    _NAV_VALID_BMS |= {f"day_{(wm + timedelta(days=i)).isoformat()}"
                       for wm in weeks for i in range(5)}

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

    # Months present per year — drives the right-edge tab links on each page
    months_by_year: dict[int, set] = defaultdict(set)
    for (yr, mo) in months:
        months_by_year[yr].add(mo)

    for year, month in months:
        # Month overview
        month_bm = f"month_{year}_{month:02d}"
        c.bookmarkPage(month_bm)
        c.addOutlineEntry(
            datetime(year, month, 1).strftime("%B %Y"), month_bm, level=0)
        draw_month_page(c, year, month, ev_by_month[(year, month)], day_week_map,
                        tz=tz, active_months=months_by_year[year])
        c.showPage()

        # Weeks that belong to this month, in chronological order
        for w in weeks_by_month.get((year, month), []):
            week_bm = f"week_{w.isoformat()}"
            c.bookmarkPage(week_bm)
            fri = w + timedelta(days=4)
            c.addOutlineEntry(
                f"Week {_sunday_week_of_year(w)} · {_fmt(w, '%-d %b')} – {_fmt(fri, '%-d %b')}",
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
                    f"  {_fmt(day_d, '%a %-d %b')}", d_bm, level=2)
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

    # Meetings agenda (very last page) — list of all meetings, linked from the
    # List nav button on every page.
    c.bookmarkPage(MEETINGS_BM)
    c.addOutlineEntry("Meetings", MEETINGS_BM, level=0)
    draw_meetings_page(c, year_val, events, event_pg,
                       active_months=active_months_set_int, tz=tz)
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
