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
import math
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


def _draw_chevron(c: canvas.Canvas, cx, cy, sz, col, left=True):
    """A single ‹ or › chevron, centred on (cx, cy)."""
    c.setStrokeColor(col)
    c.setLineWidth(1.1)
    c.setLineCap(1)
    w = sz * 0.30
    h = sz * 0.42
    if left:
        c.line(cx + w, cy + h, cx - w, cy)
        c.line(cx - w, cy, cx + w, cy - h)
    else:
        c.line(cx - w, cy + h, cx + w, cy)
        c.line(cx + w, cy, cx - w, cy - h)
    c.setLineCap(0)


def _draw_caps_label(c: canvas.Canvas, text: str, cx: float, baseline_y: float,
                     size: float = 7, col=None, cs: float = 1.5):
    """Centred, letter-spaced uppercase label (e.g. "CALENDAR", "WEEKLY PLAN").
    Wrapped in save/restore so the text-object's char-spacing doesn't leak into
    later drawString calls."""
    total_w = c.stringWidth(text, "Helvetica-Bold", size) + cs * len(text)
    c.saveState()
    to = c.beginText()
    to.setFont("Helvetica-Bold", size)
    to.setFillColor(col if col is not None else C_GREY)
    to.setCharSpace(cs)
    to.setTextOrigin(cx - total_w / 2, baseline_y)
    to.textOut(text)
    c.drawText(to)
    c.restoreState()


def _caps_left(c: canvas.Canvas, text: str, x: float, baseline_y: float,
               size: float = 8, col=None, cs: float = 1.2):
    """Left-aligned, letter-spaced uppercase section label."""
    c.saveState()
    to = c.beginText()
    to.setFont("Helvetica-Bold", size)
    to.setFillColor(col if col is not None else C_GREY)
    to.setCharSpace(cs)
    to.setTextOrigin(x, baseline_y)
    to.textOut(text)
    c.drawText(to)
    c.restoreState()


def _draw_recur_icon(c: canvas.Canvas, cx, cy, r, col):
    """A small circular ‘recurring’ arrow."""
    c.saveState()
    c.setStrokeColor(col); c.setLineWidth(0.9); c.setLineCap(1)
    start, extent = 110, 280
    p = c.beginPath()
    p.arc(cx - r, cy - r, cx + r, cy + r, start, extent)
    c.drawPath(p, stroke=1, fill=0)
    ang = math.radians(start + extent)
    ex, ey = cx + r * math.cos(ang), cy + r * math.sin(ang)
    tx, ty = -math.sin(ang), math.cos(ang)          # tangent (CCW)
    nx, ny = math.cos(ang), math.sin(ang)           # radial
    s = r * 0.95
    c.setFillColor(col)
    tri = c.beginPath()
    tri.moveTo(ex + tx * s, ey + ty * s)
    tri.lineTo(ex + nx * s * 0.6, ey + ny * s * 0.6)
    tri.lineTo(ex - nx * s * 0.6, ey - ny * s * 0.6)
    tri.close()
    c.drawPath(tri, stroke=0, fill=1)
    c.restoreState()


def _draw_pill(c: canvas.Canvas, x: float, cy: float, text: str, fill,
               icon: str = None, size: float = 6.5, cs: float = 0.8) -> float:
    """Capsule pill (fully rounded) with letter-spaced white caps text and an
    optional leading icon. Returns the pill's right-edge x for chaining."""
    pad    = 3.6 * mm
    h      = 6.2 * mm
    tw     = c.stringWidth(text, "Helvetica-Bold", size) + cs * len(text)
    icon_w = 4.2 * mm if icon else 0
    w      = pad + icon_w + tw + pad
    filled_rect(c, x, cy - h / 2, w, h, fill=fill, r=h / 2)
    if icon == "recur":
        _draw_recur_icon(c, x + pad + 1.7 * mm, cy, 1.7 * mm, C_WHITE)
    c.saveState()
    to = c.beginText()
    to.setFont("Helvetica-Bold", size)
    to.setFillColor(C_WHITE)
    to.setCharSpace(cs)
    to.setTextOrigin(x + pad + icon_w, cy - size * 0.34)
    to.textOut(text)
    c.drawText(to)
    c.restoreState()
    return x + w


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
                     omit=(), prev_bm: str = "", next_bm: str = "",
                     ctx_date: date = None, **_legacy):
    """
    Top-right navigation: Year (grid) · Month · Week · Day · List.
    Month/Week/Day are labelled tear-off calendar buttons that jump to a
    reference date's pages — TODAY by default, or `ctx_date` (e.g. a meeting's
    own date on the meeting note page).
    The List button jumps to the meetings agenda page.
    `active` highlights the current page type in the accent colour.
    `omit` is a collection of button names to drop (e.g. "year" on the year
    page, "month" on the current-month page — they would only link to self).
    `prev_bm`/`next_bm` add ‹ › arrows that step to the adjacent week/day page.
    """
    today = ctx_date or _NAV_TODAY or date.today()
    valid = _NAV_VALID_BMS
    wk    = _sunday_week_of_year(today)
    mon   = _week_monday(today)

    omit = set(omit)
    if omit_year:
        omit.add("year")

    buttons = [
        # name,    kind,   label,                          bookmark
        ("year",  "grid", "",                              YEAR_BM),
        ("month", "cal",  today.strftime("%b").upper(),    f"month_{today.year}_{today.month:02d}"),
        ("week",  "cal",  f"W{wk}",                        f"week_{mon.isoformat()}"),
        ("day",   "cal",  str(today.day),                  f"day_{today.isoformat()}"),
        ("list",  "list", "",                              MEETINGS_BM),
    ]
    if omit:
        buttons = [b for b in buttons if b[0] not in omit]

    # Optional prev/next stepper arrows (week & day pages)
    if prev_bm or next_bm:
        buttons.append(("prev", "arrow_l", "", prev_bm))
        buttons.append(("next", "arrow_r", "", next_bm))

    BH    = 6.5 * mm
    BW_C  = 9.0 * mm      # labelled calendar button
    BW_I  = 7.0 * mm      # icon-only button (year, list)
    BW_A  = 6.0 * mm      # arrow button
    GAP   = 1.8 * mm
    right = MARGIN + CONTENT_W
    top   = PAGE_H - MARGIN
    cy    = top - 13      # vertically centred in the header band

    def _bw(kind):
        if kind in ("grid", "list"):       return BW_I
        if kind in ("arrow_l", "arrow_r"): return BW_A
        return BW_C

    widths  = [_bw(k) for (_, k, _, _) in buttons]
    total_w = sum(widths) + GAP * (len(buttons) - 1)
    x = right - total_w

    for (name, kind, label, bm), bw in zip(buttons, widths):
        cx  = x + bw / 2
        col = C_ACCENT if name == active else C_GREY
        if kind == "grid":
            _draw_grid_icon(c, cx, cy, BH * 0.9, col)
        elif kind == "list":
            _draw_list_icon(c, cx, cy, BH * 0.9, col)
        elif kind == "arrow_l":
            _draw_chevron(c, cx, cy, BH, col, left=True)
        elif kind == "arrow_r":
            _draw_chevron(c, cx, cy, BH, col, left=False)
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

    # Year cap (top segment) — taps back to the year overview
    _tab_label(PAGE_H - seg_h, str(year), C_INK)
    if YEAR_BM in _NAV_VALID_BMS:
        c.linkAbsolute("", YEAR_BM, (tab_x, PAGE_H - seg_h, PAGE_W, PAGE_H))

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
                    active_months: set = None,
                    event_page_map: dict = None):
    """
    Full-page monthly calendar grid (Dayfolio style).

    • Right-edge year tab stack with the current month highlighted
    • Header: month + year, centred "CALENDAR" label
    • Columns: week-num gutter + SUNDAY … SATURDAY (full names)
    • Tall cells: day number top-right, leading/trailing days hatched, event
      pills below the number
    • Ruled notes area along the bottom
    """
    today        = datetime.now(tz).date()
    month_name   = datetime(year, month, 1).strftime("%B").upper()
    is_cur_month = (year == today.year and month == today.month)

    # ── Right-edge tab stack (current month highlighted) ──────────────────────
    _draw_year_edge_tabs(c, year, active_months=active_months, active_month=month)

    # ── Header: "MONTH year" + centred CALENDAR label ─────────────────────────
    top = PAGE_H - MARGIN
    c.setFont("Helvetica-Bold", 15)
    c.setFillColor(C_INK)
    c.drawString(MARGIN, top - 11, month_name)
    mw = c.stringWidth(month_name, "Helvetica-Bold", 15)
    txt(c, MARGIN + mw + 5, top - 11, str(year), size=15, col=C_GREY)

    # "CURRENT MONTH" pill (only on the month we're actually in)
    if is_cur_month:
        yr_w = c.stringWidth(str(year), "Helvetica", 15)
        px   = MARGIN + mw + 5 + yr_w + 4 * mm
        _draw_pill(c, px, top - 6, "CURRENT MONTH", C_CUR_PILL, size=7)

    month_bm = f"month_{year}_{month:02d}"
    week_bm  = day_week_map.get(date(year, month, 1).strftime("%Y-%m-%d"), "")
    # On the current month, the Month nav button would only link to this page
    draw_nav_buttons(c, "month", month_bm=month_bm, week_bm=week_bm,
                     omit=("month",) if is_cur_month else ())

    _draw_caps_label(c, "CALENDAR", MARGIN + CONTENT_W / 2, top - 22)

    sep_y = top - 28
    hrule(c, MARGIN, sep_y, CONTENT_W, col=C_SILVER, lw=0.6)

    # ── Event spans (multi-day events render as bars that wrap weeks) ─────────
    event_page_map = event_page_map or {}
    m_first = date(year, month, 1)
    m_last  = date(year, month, calendar.monthrange(year, month)[1])
    ev_spans: list = []
    for e in events:
        first_d = e.start.astimezone(tz).date()
        last_d  = (e.end.astimezone(tz) - timedelta(seconds=1)).date()
        if last_d < first_d:
            last_d = first_d
        seg_s = max(first_d, m_first)
        seg_e = min(last_d, m_last)
        if seg_e < seg_s:
            continue
        ev_spans.append({"s": seg_s, "e": seg_e, "ev": e})
    # Greedy lane assignment so overlapping events stack without colliding,
    # longest-running events first so they keep the top lanes across weeks.
    ev_spans.sort(key=lambda x: (x["s"], -((x["e"] - x["s"]).days), x["ev"].start))
    lane_ends: list = []
    for sp in ev_spans:
        for li in range(len(lane_ends)):
            if sp["s"] > lane_ends[li]:
                lane_ends[li] = sp["e"]; sp["lane"] = li; break
        else:
            sp["lane"] = len(lane_ends); lane_ends.append(sp["e"])

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
    cur_week_rect = None       # bounds of the row containing today (if any)
    for row, week in enumerate(cal_weeks):
        row_y_top = dow_y - row * cell_h
        row_y_bot = row_y_top - cell_h

        if is_cur_month and any(d == today for d in week):
            cur_week_rect = (day_left, row_y_bot, grid_right, row_y_top)

        # Week number, centred in the gutter, linked to the week page
        wk_num = _sunday_week_of_year(week[0])
        txt(c, MARGIN + WEEK_COL_W / 2, row_y_top - cell_h / 2 - 2,
            f"W{wk_num}", size=7, col=C_SILVER, align="center")
        row_wbm = next((day_week_map[d.isoformat()] for d in week
                        if d.isoformat() in day_week_map), "")
        if row_wbm:
            c.linkAbsolute("", row_wbm,
                           (grid_left, row_y_bot, day_left, row_y_top))

        # Cell backgrounds, day numbers and whole-cell tap targets
        for col, d in enumerate(week):
            cx        = day_left + col * WKDAY_W
            in_month  = (d.month == month and d.year == year)
            is_today  = (d == today)

            if not in_month:
                # Adjacent-month day: hatch the cell, grey number, no events
                hatch_rect(c, cx, row_y_bot, WKDAY_W, cell_h,
                           col=C_SILVER, gap=2.6)
                txt(c, cx + WKDAY_W - 2.5 * mm, row_y_top - 5 * mm,
                    str(d.day), size=8, col=C_SILVER, align="right")
                continue

            # Today's cell gets a light wash behind its content
            if is_today:
                filled_rect(c, cx, row_y_bot, WKDAY_W, cell_h, fill=C_ACCENT_LT)

            # Day number (top-right)
            num_x = cx + WKDAY_W - 2.5 * mm
            num_y = row_y_top - 5 * mm
            if is_today:
                circle(c, cx + WKDAY_W - 4 * mm, num_y + 1.3 * mm,
                       4 * mm, fill=C_TODAY_NAVY)
                txt(c, num_x, num_y, str(d.day),
                    size=8, bold=True, col=C_WHITE, align="right")
            else:
                txt(c, num_x, num_y, str(d.day),
                    size=8, bold=False, col=C_INK, align="right")

            # Whole-cell tap target → weekly page
            dk = d.isoformat()
            if dk in day_week_map:
                c.linkAbsolute("", day_week_map[dk],
                               (cx, row_y_bot, cx + WKDAY_W, row_y_top))

        # ── Event bars for this week (multi-day spans wrap at week edges) ─────
        BAR_H   = 4 * mm
        BAR_GAP = 1.2 * mm
        unit    = BAR_H + BAR_GAP
        lane0_y = row_y_top - 8.5 * mm            # top edge of the lane-0 bar
        week_s, week_e = week[0], week[6]
        overflow: dict = {}
        for sp in ev_spans:
            if sp["e"] < week_s or sp["s"] > week_e:
                continue
            o_s   = max(sp["s"], week_s)
            o_e   = min(sp["e"], week_e)
            col_s = (o_s - week_s).days
            col_e = (o_e - week_s).days
            bar_top = lane0_y - sp["lane"] * unit
            if bar_top - BAR_H < row_y_bot + 1.5 * mm:
                dd = o_s
                while dd <= o_e:
                    overflow[dd] = overflow.get(dd, 0) + 1
                    dd += timedelta(days=1)
                continue
            x0 = day_left + col_s * WKDAY_W + 0.6 * mm
            x1 = day_left + (col_e + 1) * WKDAY_W - 0.6 * mm
            ec = _event_color(sp["ev"].title)
            filled_rect(c, x0, bar_top - BAR_H, x1 - x0, BAR_H, fill=ec, r=1.5)
            txt(c, x0 + 1.5 * mm, bar_top - BAR_H + 1.2 * mm,
                sp["ev"].title, size=7, bold=True, col=C_WHITE,
                max_w=x1 - x0 - 3 * mm)
            if sp["ev"].id in event_page_map:
                c.linkAbsolute("", f"event_{sp['ev'].id}",
                               (x0, bar_top - BAR_H, x1, bar_top))

        # Overflow "+N" markers for days that ran out of lane room
        for col, d in enumerate(week):
            if overflow.get(d):
                cx = day_left + col * WKDAY_W
                txt(c, cx + 1.5 * mm, row_y_bot + 1.5 * mm,
                    f"+{overflow[d]}", size=6, col=C_GREY)

    # ── Grid lines (on top of cell content) ───────────────────────────────────
    for r in range(n_weeks + 1):
        y = dow_y - r * cell_h
        hrule(c, grid_left, y, CONTENT_W, col=C_GHOST, lw=0.5)
    for col in range(8):
        vx = day_left + col * WKDAY_W
        vrule(c, vx, grid_bot, body_h, col=C_GHOST, lw=0.5)
    vrule(c, grid_left, grid_bot, body_h, col=C_GHOST, lw=0.5)   # outer-left

    # ── Current-week emphasis (dotted outline around this week's row) ──────────
    if cur_week_rect:
        x0, y0, x1, y1 = cur_week_rect
        c.saveState()
        c.setStrokeColor(C_GREY)
        c.setLineWidth(0.9)
        c.setDash([1.4, 1.4])
        c.rect(x0, y0, x1 - x0, y1 - y0, fill=0, stroke=1)
        c.setDash([])
        c.restoreState()

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
                   month_bookmark: str = "",
                   prev_week_bm: str = "",
                   next_week_bm: str = "",
                   prev_stats: dict = None):
    """
    Dayfolio-style weekly plan: a Mon–Fri timed grid above a planning area
    (FOCUS · PRIORITIES / HABITS · WEEK STATS / TO DO LIST · NOTES).
    """
    week_friday = week_monday + timedelta(days=4)
    month       = week_monday.month
    week_num    = _sunday_week_of_year(week_monday)
    today       = datetime.now(tz).date()

    draw_tab(c, month, month_bookmark=month_bookmark)

    # ── Header: month + year, stacked week number, centred WEEKLY PLAN ─────────
    top = PAGE_H - MARGIN
    month_str = week_monday.strftime("%B").upper()
    c.setFont("Helvetica-Bold", 15)
    c.setFillColor(C_INK)
    c.drawString(MARGIN, top - 11, month_str)
    mw = c.stringWidth(month_str, "Helvetica-Bold", 15)
    txt(c, MARGIN + mw + 5, top - 11, str(week_monday.year), size=15, col=C_GREY)
    yr_w = c.stringWidth(str(week_monday.year), "Helvetica", 15)
    wk_x = MARGIN + mw + 5 + yr_w + 5 * mm
    txt(c, wk_x, top - 5, "WEEK", size=5.5, bold=True, col=C_GREY)
    txt(c, wk_x, top - 13, str(week_num), size=11, bold=True, col=C_INK)

    _draw_caps_label(c, "WEEKLY PLAN", MARGIN + CONTENT_W / 2, top - 22)

    week_bm      = f"week_{week_monday.isoformat()}"
    first_day_bm = f"day_{week_monday.isoformat()}"
    is_cur_week  = (week_monday <= today <= week_friday)
    draw_nav_buttons(c, "week", month_bm=month_bookmark, week_bm=week_bm,
                     day_bm=first_day_bm,
                     omit=("week",) if is_cur_week else (),
                     prev_bm=prev_week_bm, next_bm=next_week_bm)

    sep_y = top - 28
    hrule(c, MARGIN, sep_y, CONTENT_W, col=C_SILVER, lw=0.6)

    # ── Sub-header: WEEK n · date range  + status pill ────────────────────────
    sub_y = sep_y - 6 * mm
    if week_monday.month == week_friday.month:
        rng = (f"{week_monday.day}–{week_friday.day} "
               f"{week_monday.strftime('%b').upper()}")
    else:
        rng = (f"{week_monday.day} {week_monday.strftime('%b').upper()} – "
               f"{week_friday.day} {week_friday.strftime('%b').upper()}")
    sub_label = f"WEEK {week_num}  ·  {rng}"
    txt(c, MARGIN, sub_y, sub_label, size=8.5, bold=True, col=C_INK_2)

    delta_w = (week_monday - _week_monday(today)).days // 7
    pill = (("THIS WEEK", C_ACCENT) if delta_w == 0 else
            ("NEXT WEEK", colors.HexColor("#3FA79F")) if delta_w == 1 else None)
    if pill:
        ptext, pcol = pill
        psx = MARGIN + c.stringWidth(sub_label, "Helvetica-Bold", 8.5) + 4 * mm
        _draw_pill(c, psx, sub_y + 1.1 * mm, ptext, pcol)

    # ── Geometry ──────────────────────────────────────────────────────────────
    TIME_COL_W = 12 * mm
    DAY_COL_W  = (CONTENT_W - TIME_COL_W) / 5
    DAY_HDR_H  = 13 * mm
    BOTTOM_H   = 126 * mm

    days = [week_monday + timedelta(days=i) for i in range(5)]

    hdr_top       = sub_y - 4 * mm
    hdr_bot       = hdr_top - DAY_HDR_H
    time_grid_top = hdr_bot
    grid_bot      = MARGIN + BOTTOM_H
    grid_h        = time_grid_top - grid_bot
    n_hours = HOUR_END - HOUR_START
    slot_h  = grid_h / n_hours

    # ── Day-column header band (weekday + number) ─────────────────────────────
    filled_rect(c, MARGIN + TIME_COL_W, hdr_bot,
                CONTENT_W - TIME_COL_W, DAY_HDR_H, fill=C_WKND)
    for i, day in enumerate(days):
        cx    = MARGIN + TIME_COL_W + i * DAY_COL_W
        cxm   = cx + DAY_COL_W / 2
        is_td = (day == today)
        # Today's column highlight (header + grid)
        if is_td:
            filled_rect(c, cx, grid_bot, DAY_COL_W,
                        hdr_top - grid_bot, fill=C_ACCENT_LT)
        if i > 0:
            vrule(c, cx, grid_bot, hdr_top - grid_bot, col=C_GHOST, lw=0.4)
        txt(c, cxm, hdr_top - 4 * mm, day.strftime("%a").upper(),
            size=7, bold=True, col=C_GREY, align="center")
        txt(c, cxm, hdr_top - 11 * mm, str(day.day),
            size=15, bold=True, col=C_ACCENT if is_td else C_INK, align="center")
        c.linkAbsolute("", f"day_{day.isoformat()}",
                       (cx, hdr_bot, cx + DAY_COL_W, hdr_top))
    hrule(c, MARGIN + TIME_COL_W, hdr_bot, CONTENT_W - TIME_COL_W,
          col=C_SILVER, lw=0.5)

    # ── Time grid ────────────────────────────────────────────────────────────
    for i in range(n_hours + 1):
        hour = HOUR_START + i
        y    = time_grid_top - i * slot_h

        # Hour label (e.g. 7AM / 12PM / 6PM), right-aligned into the gutter
        if i < n_hours:
            hr_lbl = ("12PM" if hour == 12 else
                      f"{hour - 12}PM" if hour > 12 else f"{hour}AM")
            txt(c, MARGIN + TIME_COL_W - 1.5 * mm, y - 4,
                hr_lbl, size=8, col=C_GREY, align="right")

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

    # ── Planning area (FOCUS·PRIORITIES / HABITS·WEEK STATS / TO DO·NOTES) ─────
    stats = _week_meeting_stats(days, days_events, tz)
    _draw_week_bottom(c, MARGIN, MARGIN + BOTTOM_H, CONTENT_W, stats, prev_stats)


_STAT_GREEN = colors.HexColor("#2E9E5B")


def _week_meeting_stats(days, days_events, tz) -> dict:
    """Timed-meeting tallies for a Mon–Fri week: count, total hours, per-day."""
    per_day, total_h, n = [], 0.0, 0
    for day in days:
        dh = 0.0
        for e in days_events.get(day.strftime("%Y-%m-%d"), []):
            if e.is_all_day:
                continue
            dur = (e.end - e.start).total_seconds() / 3600.0
            if dur <= 0:
                continue
            dh += dur; total_h += dur; n += 1
        per_day.append(dh)
    return {"n": n, "total_h": total_h, "per_day": per_day}


def _stats_compare(cur_n: int, prev_stats: dict):
    """(text, colour, direction) comparing this week's meeting count to last."""
    prev_n = (prev_stats or {}).get("n", 0)
    if not prev_stats or prev_n == 0:
        return ("no data from last week", C_GREY, None)
    if cur_n == prev_n:
        return ("same as last week", C_GREY, None)
    pct = round(abs(cur_n - prev_n) / prev_n * 100)
    if cur_n < prev_n:
        return (f"{pct}% less meetings vs last week", _STAT_GREEN, "down")
    return (f"{pct}% more meetings vs last week", _STAT_GREEN, "up")


def _draw_check(c, cx, cy, sz, col):
    c.setStrokeColor(col); c.setLineWidth(1.0); c.setLineCap(1)
    c.line(cx - sz * 0.4, cy, cx - sz * 0.05, cy - sz * 0.4)
    c.line(cx - sz * 0.05, cy - sz * 0.4, cx + sz * 0.5, cy + sz * 0.5)
    c.setLineCap(0)


def _draw_tri(c, cx, cy, sz, col, up=False):
    c.setFillColor(col)
    p = c.beginPath()
    if up:
        p.moveTo(cx, cy + sz * 0.5)
        p.lineTo(cx - sz * 0.45, cy - sz * 0.4)
        p.lineTo(cx + sz * 0.45, cy - sz * 0.4)
    else:
        p.moveTo(cx, cy - sz * 0.5)
        p.lineTo(cx - sz * 0.45, cy + sz * 0.4)
        p.lineTo(cx + sz * 0.45, cy + sz * 0.4)
    p.close()
    c.drawPath(p, fill=1, stroke=0)


def _fmt_hours(v: float) -> str:
    return f"{v:.0f}h" if abs(v - round(v)) < 0.05 else f"{v:.1f}h"


def _draw_week_bottom(c: canvas.Canvas, x: float, top_y: float, width: float,
                      stats: dict, prev_stats: dict):
    """
    Three-row planning grid beneath the weekly time grid:

      ┌ FOCUS (pink box) ──────┬ PRIORITIES (1·2·3) ───────────┐
      ├ HABITS (S M T W T F S) ┼ WEEK STATS (+ mini bar chart) ┤
      └ TO DO LIST ────────────┴ NOTES ────────────────────────┘
    """
    bot   = MARGIN
    mid   = x + width / 2
    pad   = 3 * mm
    rose  = C_CUR_PILL
    line_h = 7.0 * mm

    r1_bot = top_y - 30 * mm        # FOCUS / PRIORITIES band
    r2_bot = r1_bot - 46 * mm       # HABITS / WEEK STATS band

    # Boundary + divider lines
    hrule(c, x, top_y,  width, col=C_SILVER, lw=0.6)
    hrule(c, x, r1_bot, width, col=C_GHOST,  lw=0.5)
    hrule(c, x, r2_bot, width, col=C_SILVER, lw=0.6)
    vrule(c, mid, bot,    r2_bot - bot,      col=C_GHOST, lw=0.5)
    vrule(c, mid, r2_bot, r1_bot - r2_bot,   col=C_GHOST, lw=0.5)

    # ── Row 1 · FOCUS box ─────────────────────────────────────────────────────
    fbx, fby = x + pad, r1_bot + 3 * mm
    fbw, fbh = mid - pad - fbx, (top_y - 3 * mm) - (r1_bot + 3 * mm)
    filled_rect(c, fbx, fby, fbw, fbh, fill=colors.HexColor("#F6DDE4"), r=3)
    c.saveState()
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(colors.HexColor("#B06A80"))
    c.translate(fbx + 4 * mm, fby + fbh / 2)
    c.rotate(90)
    c.drawCentredString(0, 0, "FOCUS")
    c.restoreState()

    # ── Row 1 · PRIORITIES ────────────────────────────────────────────────────
    px = mid + pad
    pw = x + width - px - pad
    txt(c, px, top_y - 5 * mm, "PRIORITIES", size=9, bold=True, col=C_GREY)
    cr = 3.2 * mm
    cy = top_y - 12 * mm
    for n in range(1, 4):
        circle(c, px + cr, cy, cr, fill=rose)
        c.setFont("Helvetica-Bold", 7.5)
        c.setFillColor(C_WHITE)
        c.drawCentredString(px + cr, cy - 2.6, str(n))
        hrule(c, px + 2 * cr + 2 * mm, cy - cr + 1 * mm,
              pw - 2 * cr - 2 * mm, col=C_GHOST, lw=0.5)
        cy -= 7 * mm

    # ── Row 2 · HABITS ────────────────────────────────────────────────────────
    txt(c, x + pad, r1_bot - 5 * mm, "HABITS", size=9, bold=True, col=C_GREY)
    dow = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]
    hr_r, hr_gap = 1.7 * mm, 1.2 * mm
    block_w = 7 * (2 * hr_r) + 6 * hr_gap
    circ_x0 = mid - pad - block_w
    hdr_y   = r1_bot - 9 * mm
    for di, dn in enumerate(dow):
        ccx = circ_x0 + hr_r + di * (2 * hr_r + hr_gap)
        txt(c, ccx, hdr_y, dn, size=4.5, col=C_GREY, align="center")
    n_rows  = 5
    row_gap = (r1_bot - 12 * mm - (r2_bot + 2 * mm)) / n_rows
    ry = r1_bot - 12 * mm - row_gap / 2
    for _ in range(n_rows):
        hrule(c, x + pad, ry - 1.5 * mm,
              circ_x0 - hr_r - 2 * mm - (x + pad), col=C_GHOST, lw=0.5)
        for di in range(7):
            ccx = circ_x0 + hr_r + di * (2 * hr_r + hr_gap)
            circle(c, ccx, ry, hr_r, fill=C_WHITE, stroke=C_SILVER, lw=0.5)
        ry -= row_gap

    # ── Row 2 · WEEK STATS ────────────────────────────────────────────────────
    sx = mid + pad
    sw = x + width - sx - pad
    txt(c, sx, r1_bot - 5 * mm, "WEEK STATS", size=9, bold=True, col=C_GREY)
    n, total_h = stats["n"], stats["total_h"]
    sy = r1_bot - 11 * mm
    if n == 0:
        txt(c, sx, sy, "No events this week", size=7.5, italic=True, col=C_GREY)
        sy -= 6 * mm
    else:
        txt(c, sx, sy, f"{n} invite{'s' if n != 1 else ''} · "
            f"{_fmt_hours(total_h)} total", size=7.5, col=C_INK_2); sy -= 5.5 * mm
        txt(c, sx, sy, f"{n} meeting{'s' if n != 1 else ''} · "
            f"{_fmt_hours(total_h)}", size=7.5, col=C_INK_2); sy -= 5.5 * mm
        _draw_check(c, sx + 1.3 * mm, sy + 1 * mm, 2.2 * mm, _STAT_GREEN)
        txt(c, sx + 4 * mm, sy, f"{_fmt_hours(total_h)} accepted ({n})",
            size=7.5, col=_STAT_GREEN); sy -= 6 * mm
    cmp_text, cmp_col, direction = _stats_compare(n, prev_stats)
    if direction:
        _draw_tri(c, sx + 1.3 * mm, sy + 1 * mm, 2.2 * mm, cmp_col,
                  up=(direction == "up"))
        txt(c, sx + 4 * mm, sy, cmp_text, size=7.5, col=cmp_col)
    else:
        txt(c, sx, sy, cmp_text, size=7.5, italic=True, col=cmp_col)

    # Mini bar chart of meeting-hours per weekday (Mon–Fri)
    per = stats["per_day"]
    if any(per):
        labels = ["MON", "TUE", "WED", "THU", "FRI"]
        ch_bot, ch_h = r2_bot + 6 * mm, 7 * mm
        col_w = sw / 5
        bw    = col_w * 0.46
        mx    = max(per) or 1
        for di in range(5):
            colx = sx + di * col_w + (col_w - bw) / 2
            bh   = (per[di] / mx) * ch_h
            if bh > 0.3:
                filled_rect(c, colx, ch_bot, bw, bh, fill=rose, r=1)
            txt(c, colx + bw / 2, ch_bot - 3.5 * mm, labels[di],
                size=4.5, col=C_SILVER, align="center")

    # ── Row 3 · TO DO LIST | NOTES ────────────────────────────────────────────
    txt(c, x + pad, r2_bot - 5 * mm, "TO DO LIST", size=9, bold=True, col=C_GREY)
    cbr = 2.4 * mm
    ty  = r2_bot - 11 * mm
    while ty > bot + 2 * mm:
        circle(c, x + pad + cbr, ty, cbr, fill=C_WHITE, stroke=C_SILVER, lw=0.5)
        hrule(c, x + pad + 2 * cbr + 2 * mm, ty - cbr,
              mid - pad - (x + pad + 2 * cbr + 2 * mm), col=C_GHOST, lw=0.5)
        ty -= line_h

    txt(c, mid + pad, r2_bot - 5 * mm, "NOTES", size=9, bold=True, col=C_GREY)
    ny = r2_bot - 11 * mm
    while ny > bot + 2 * mm:
        hrule(c, mid + pad, ny, x + width - (mid + pad) - pad,
              col=C_GHOST, lw=0.5)
        ny -= line_h


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


def _relative_day_pill(target: date, today: date):
    """(label, colour) describing target relative to today, or None.
    TODAY/TOMORROW/YESTERDAY take precedence over THIS/LAST/NEXT WEEK."""
    dd = (target - today).days
    dw = (_week_monday(target) - _week_monday(today)).days // 7
    if dd == 0:  return ("TODAY",     C_TODAY_NAVY)
    if dd == 1:  return ("TOMORROW",  colors.HexColor("#2E9E5B"))
    if dd == -1: return ("YESTERDAY", colors.HexColor("#E08A3C"))
    if dw == 0:  return ("THIS WEEK", C_ACCENT)
    if dw == -1: return ("LAST WEEK", colors.HexColor("#9AA0A6"))
    if dw == 1:  return ("NEXT WEEK", colors.HexColor("#3FA79F"))
    return None


def draw_day_page(c: canvas.Canvas, day_date: date, events: list,
                  event_page_map: dict, tz,
                  week_bookmark: str = "", month_bookmark: str = "",
                  prev_day_bm: str = "", next_day_bm: str = ""):
    """
    Dayfolio-style day view: left time-grid, right planning panel.
    Left  ~38%: hour slots 7AM–6PM with timed event blocks
    Right ~62%: Quote · FOCUS box · PRIORITIES · TO DO LIST · NOTES
    """
    today = datetime.now(tz).date()
    month = day_date.month
    is_today = (day_date == today)

    draw_tab(c, month, month_bookmark=month_bookmark)

    # ── Header: month + year, stacked week number, centred SCHEDULE label ──────
    top = PAGE_H - MARGIN
    month_str = day_date.strftime("%B").upper()
    c.setFont("Helvetica-Bold", 15)
    c.setFillColor(C_INK)
    c.drawString(MARGIN, top - 11, month_str)
    mw = c.stringWidth(month_str, "Helvetica-Bold", 15)
    txt(c, MARGIN + mw + 5, top - 11, str(day_date.year), size=15, col=C_GREY)
    yr_w = c.stringWidth(str(day_date.year), "Helvetica", 15)
    wk_x = MARGIN + mw + 5 + yr_w + 5 * mm
    txt(c, wk_x, top - 5, "WEEK", size=5.5, bold=True, col=C_GREY)
    txt(c, wk_x, top - 13, str(_sunday_week_of_year(day_date)),
        size=11, bold=True, col=C_INK)

    _draw_caps_label(c, "SCHEDULE", MARGIN + CONTENT_W / 2, top - 22)

    day_bm = f"day_{day_date.isoformat()}"
    draw_nav_buttons(c, "day", month_bm=month_bookmark, week_bm=week_bookmark,
                     day_bm=day_bm, omit=("day",) if is_today else (),
                     prev_bm=prev_day_bm, next_bm=next_day_bm)

    sep_y = top - 28
    hrule(c, MARGIN, sep_y, CONTENT_W, col=C_SILVER, lw=0.6)

    # ── Sub-header: weekday, month day  + relative-time status pill ────────────
    sub_y     = sep_y - 6 * mm
    day_label = _fmt(day_date, "%A, %B %-d").upper()
    txt(c, MARGIN, sub_y, day_label, size=10.5, bold=True, col=C_INK)

    pill = _relative_day_pill(day_date, today)
    if pill:
        ptext, pcol = pill
        psx = MARGIN + c.stringWidth(day_label, "Helvetica-Bold", 10.5) + 4 * mm
        _draw_pill(c, psx, sub_y + 1.3 * mm, ptext, pcol)

    # ── Column geometry ────────────────────────────────────────────────────────
    SPLIT    = 0.38                         # time grid fraction of content width
    TIME_W   = CONTENT_W * SPLIT
    PLAN_W   = CONTENT_W - TIME_W
    time_x   = MARGIN
    plan_x   = MARGIN + TIME_W
    grid_top = sub_y - 5 * mm
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

    # Rose accent bar to the left of the quote
    q_top = py - 4 * mm
    q_bot = py - (19 if qline2 else 15) * mm - 1 * mm
    filled_rect(c, plan_x + pad, q_bot, 2, q_top - q_bot, fill=C_CUR_PILL)
    qx = plan_x + pad + 3 * mm
    txt(c, qx, py - 6 * mm, qline1, size=9, italic=True, col=C_INK_2)
    if qline2:
        txt(c, qx, py - 12.5 * mm, qline2, size=9, italic=True, col=C_INK_2)
    txt(c, qx, py - (19 if qline2 else 15) * mm,
        f"— {author.upper()}", size=7, bold=True, col=C_CUR_PILL)

    py -= (20 if qline2 else 17) * mm

    # FOCUS box (solid pink, rotated label, no inner rules)
    FOCUS_H = 36 * mm
    filled_rect(c, plan_x + pad, py - FOCUS_H, PLAN_W - 2 * pad, FOCUS_H,
                fill=colors.HexColor("#E7BFC9"), r=4)
    c.saveState()
    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(colors.HexColor("#9C6072"))
    c.translate(plan_x + pad + 5 * mm, py - FOCUS_H / 2)
    c.rotate(90)
    c.drawCentredString(0, 0, "FOCUS")
    c.restoreState()

    py -= FOCUS_H + 5 * mm

    # PRIORITIES — three rose-graded circles
    txt(c, plan_x + pad, py, "PRIORITIES", size=9, bold=True, col=C_GREY)
    py -= 7 * mm
    circ_r = 4 * mm
    prio_cols = [colors.HexColor("#A23E4E"), colors.HexColor("#CC5E70"),
                 colors.HexColor("#E59AA8")]
    for n in range(1, 4):
        cx2 = plan_x + pad + circ_r
        circle(c, cx2, py, circ_r, fill=prio_cols[n - 1])
        c.setFont("Helvetica-Bold", 9); c.setFillColor(C_WHITE)
        c.drawCentredString(cx2, py - 3, str(n))
        hrule(c, cx2 + circ_r + 2 * mm, py - 1 * mm,
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
                      week_bookmark: str, tz=None, day_bookmark: str = "",
                      self_email: str = None):
    tz  = tz or timezone.utc
    ls  = event.start.astimezone(tz)
    le  = event.end.astimezone(tz)
    year, month = ls.year, ls.month
    today = datetime.now(tz).date()
    rose  = C_CUR_PILL

    # Right-edge month tab stack with the meeting's month highlighted
    active = {m for m in range(1, 13)
              if f"month_{year}_{m:02d}" in _NAV_VALID_BMS}
    _draw_year_edge_tabs(c, year, active_months=active or None,
                         active_month=month)

    top = PAGE_H - MARGIN
    pill_cy = top - 9

    # ── Header pills: MEETING NOTES + relative-time pill (+ SERIES) ────────────
    plx = _draw_pill(c, MARGIN, pill_cy, "MEETING NOTES", rose)
    rp = _relative_day_pill(ls.date(), today)
    if rp:
        _draw_pill(c, plx + 2 * mm, pill_cy, rp[0], rp[1])
    # SERIES pill (recurring events), centred in the header row
    if getattr(event, "is_recurring", False):
        stext = "SERIES"
        sw = 3.6 * mm + 4.2 * mm + (c.stringWidth(stext, "Helvetica-Bold", 6.5)
                                    + 0.8 * len(stext)) + 3.6 * mm
        _draw_pill(c, MARGIN + CONTENT_W / 2 - sw / 2, pill_cy, stext, rose,
                   icon="recur")

    # Nav points at the meeting's own month/week/day (ctx_date)
    day_bm = day_bookmark or f"day_{ls.date().isoformat()}"
    draw_nav_buttons(c, "", month_bm=f"month_{year}_{month:02d}",
                     week_bm=week_bookmark, day_bm=day_bm, ctx_date=ls.date())

    # ── Title row: event-colour marker + title (clear of the pills above) ─────
    ty = top - 34
    filled_rect(c, MARGIN, ty - 0.5 * mm, 4 * mm, 4 * mm,
                fill=_event_color(event.title), r=1.2)
    c.setFont("Helvetica-Bold", 16)
    c.setFillColor(C_INK)
    c.drawString(MARGIN + 6 * mm, ty, event.title)

    # Subline: date · time–time · duration
    date_str = _fmt(ls, "%A, %B %-d, %Y").upper()
    if event.is_all_day:
        sub = f"{date_str}   ·   All day"
    else:
        sub = (f"{date_str}   ·   {ls.hour}:{ls.minute:02d} – "
               f"{le.hour}:{le.minute:02d}   ·   {event.duration_str}")
    txt(c, MARGIN, ty - 6.5 * mm, sub, size=8, col=C_GREY)

    y = ty - 11 * mm
    hrule(c, MARGIN, y, CONTENT_W, col=rose, lw=0.8)
    y -= 6 * mm

    # Reserve an ACTION ITEMS block at the bottom
    AI_ROWS   = 5
    ai_line_h = 7.2 * mm
    ai_label_y = MARGIN + AI_ROWS * ai_line_h + 2 * mm

    # ── ATTENDEES ─────────────────────────────────────────────────────────────
    if event.attendees:
        _caps_left(c, f"ATTENDEES ({len(event.attendees)})", MARGIN, y)
        y -= 6 * mm
        _RESP = {
            "accepted":  colors.HexColor("#4CAF50"),
            "tentative": colors.HexColor("#FF9800"),
            "declined":  colors.HexColor("#F44336"),
        }
        for att in event.attendees:
            if y < ai_label_y + 30 * mm:
                break
            name = att.name or att.email
            ring = _RESP.get(att.response, C_SILVER)
            circle(c, MARGIN + 1.7 * mm, y + 1 * mm, 1.7 * mm,
                   fill=C_WHITE, stroke=ring, lw=0.9)
            txt(c, MARGIN + 5.5 * mm, y, name, size=8, col=C_INK_2)
            if self_email and att.email and att.email.lower() == self_email.lower():
                nw = c.stringWidth(name, "Helvetica", 8)
                chx = MARGIN + 5.5 * mm + nw + 2 * mm
                cw  = c.stringWidth("you", "Helvetica", 6) + 3 * mm
                filled_rect(c, chx, y - 0.6 * mm, cw, 3.8 * mm,
                            fill=C_ACCENT_LT, r=1.6)
                txt(c, chx + cw / 2, y, "you", size=6, col=C_INK_2, align="center")
            y -= 5 * mm
        y -= 2 * mm
        hrule(c, MARGIN, y, CONTENT_W, col=rose, lw=0.8)
        y -= 6 * mm

    # ── DESCRIPTION ───────────────────────────────────────────────────────────
    desc = _strip_teams_boilerplate(event.description)
    if desc:
        _caps_left(c, "DESCRIPTION", MARGIN, y)
        y -= 6 * mm
        words, line, n_lines = desc.split(), "", 0
        for word in words:
            test = (line + " " + word).strip()
            if c.stringWidth(test, "Helvetica", 8.5) < CONTENT_W:
                line = test
            else:
                txt(c, MARGIN, y, line, size=8.5, col=C_INK_2)
                y -= 5.5 * mm; line = word; n_lines += 1
                if n_lines >= 6:
                    line = ""; break
        if line:
            txt(c, MARGIN, y, line, size=8.5, col=C_INK_2)
            y -= 5.5 * mm
        y -= 2 * mm
        hrule(c, MARGIN, y, CONTENT_W, col=rose, lw=0.8)
        y -= 6 * mm

    # ── NOTES (large ruled writing area) ──────────────────────────────────────
    _caps_left(c, "NOTES", MARGIN, y)
    y -= 9 * mm
    while y > ai_label_y + 6 * mm:
        hrule(c, MARGIN, y, CONTENT_W, col=C_GHOST, lw=0.5)
        y -= 9 * mm

    # ── ACTION ITEMS (checkbox rows at the bottom) ────────────────────────────
    _caps_left(c, "ACTION ITEMS", MARGIN, ai_label_y)
    ar = 2.4 * mm
    ay = ai_label_y - 6 * mm
    for _ in range(AI_ROWS):
        circle(c, MARGIN + ar, ay, ar, fill=C_WHITE, stroke=C_SILVER, lw=0.6)
        hrule(c, MARGIN + 2 * ar + 2 * mm, ay - ar,
              CONTENT_W - (2 * ar + 2 * mm), col=C_GHOST, lw=0.5)
        ay -= ai_line_h


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
    self_email: str = None,
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

    # Weeks (Mon–Fri). Cover every week shown in the month grids — not just the
    # start→end range — so each week indicator on a month page is clickable.
    first_month_day = date(months[0][0], months[0][1], 1)
    last_y, last_m  = months[-1]
    last_month_day  = date(last_y, last_m, calendar.monthrange(last_y, last_m)[1])
    first_mon = _week_monday(min(start_date, first_month_day))
    last_mon  = _week_monday(max(end_date, last_month_day))
    weeks = []
    w = first_mon
    while w <= last_mon:
        weeks.append(w)
        w += timedelta(days=7)
    week_set = set(weeks)

    # Nav context: today + the set of bookmarks that actually exist, so the
    # top "jump to today" calendar buttons only link to real pages.
    global _NAV_TODAY, _NAV_VALID_BMS
    _NAV_TODAY = datetime.now(tz).date()
    _NAV_VALID_BMS = {YEAR_BM, MEETINGS_BM}
    _NAV_VALID_BMS |= {f"month_{y}_{m:02d}" for (y, m) in months}
    _NAV_VALID_BMS |= {f"week_{wm.isoformat()}" for wm in weeks}
    _NAV_VALID_BMS |= {f"day_{(wm + timedelta(days=i)).isoformat()}"
                       for wm in weeks for i in range(5)}

    # Group events by day (month pages filter the full list themselves so that
    # events spanning a month boundary still render on both months)
    ev_by_day: dict[str, list] = {}
    for e in events:
        loc = e.start.astimezone(tz)
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
        draw_month_page(c, year, month, events, day_week_map,
                        tz=tz, active_months=months_by_year[year],
                        event_page_map=event_pg)
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
            # Prev/next week stepper targets (only when those pages exist)
            prev_w = w - timedelta(days=7)
            next_w = w + timedelta(days=7)
            prev_week_bm = f"week_{prev_w.isoformat()}" if prev_w in week_set else ""
            next_week_bm = f"week_{next_w.isoformat()}" if next_w in week_set else ""
            # Previous week's meeting stats → WEEK STATS comparison line
            prev_days  = [prev_w + timedelta(days=i) for i in range(5)]
            prev_evts  = {d.strftime("%Y-%m-%d"):
                          ev_by_day.get(d.strftime("%Y-%m-%d"), [])
                          for d in prev_days}
            prev_stats = _week_meeting_stats(prev_days, prev_evts, tz)
            draw_week_page(c, w, wk_evts, event_pg, tz, month_bookmark=month_bm,
                           prev_week_bm=prev_week_bm, next_week_bm=next_week_bm,
                           prev_stats=prev_stats)
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
                # Prev/next day steppers (Mon–Fri chain across weeks)
                if i > 0:
                    prev_day_bm = f"day_{(day_d - timedelta(days=1)).isoformat()}"
                else:
                    pw = w - timedelta(days=7)
                    prev_day_bm = (f"day_{(pw + timedelta(days=4)).isoformat()}"
                                   if pw in week_set else "")
                if i < 4:
                    next_day_bm = f"day_{(day_d + timedelta(days=1)).isoformat()}"
                else:
                    nw = w + timedelta(days=7)
                    next_day_bm = f"day_{nw.isoformat()}" if nw in week_set else ""
                draw_day_page(c, day_d, day_ev, event_pg, tz,
                              week_bookmark=week_bm,
                              month_bookmark=month_bm,
                              prev_day_bm=prev_day_bm, next_day_bm=next_day_bm)
                c.showPage()

            # Event detail pages for this week, sorted by start time
            for e in sorted(events_by_week.get(w, []), key=lambda e: e.start):
                event_bm = f"event_{e.id}"
                c.bookmarkPage(event_bm)
                c.addOutlineEntry(f"    {e.title[:40]}", event_bm, level=3)
                draw_meeting_page(c, e, event_wk_bm[e.id], tz=tz,
                                  day_bookmark=event_day_bm.get(e.id, ""),
                                  self_email=self_email)
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
