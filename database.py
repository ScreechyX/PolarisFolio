"""
Database layer. SQLite via aiosqlite.

Tables:
  settings      - key/value app config (MS client ID, RM token, etc.)
  ical_feeds    - saved ICS feed URLs
  uploads       - history of generated planners
  meeting_slots - stable event_id -> page slot map for the rolling yearly
                  planner, so a meeting's note page (and its handwriting)
                  keeps a fixed position even when the meeting moves
"""

import os
import json
import aiosqlite
from datetime import datetime

DB_PATH = os.environ.get("POLARISFOLIO_DB", os.path.expanduser("~/.polarisfolio_web.db"))


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS ical_feeds (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                url        TEXT NOT NULL,
                enabled    INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS uploads (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name TEXT NOT NULL,
                start_date   TEXT NOT NULL,
                end_date     TEXT NOT NULL,
                event_count  INTEGER DEFAULT 0,
                pdf_path     TEXT,
                uploaded_to_rm INTEGER DEFAULT 0,
                sync_action  TEXT,
                created_at   TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS meeting_slots (
                year      INTEGER NOT NULL,
                event_id  TEXT NOT NULL,
                slot      INTEGER NOT NULL,
                title     TEXT,
                last_seen TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (year, event_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_meeting_slots_year_slot
                ON meeting_slots (year, slot);
        """)
        # Migration: add sync_action to uploads tables created before it existed.
        try:
            await db.execute("ALTER TABLE uploads ADD COLUMN sync_action TEXT")
        except Exception:
            pass  # column already present
        await db.commit()


# -- Settings ----------------------------------------------------------------

async def get_setting(key: str, default=None):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            if row:
                return row[0]
            return default


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )
        await db.commit()


async def get_all_settings() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT key, value FROM settings") as cur:
            rows = await cur.fetchall()
            return {r[0]: r[1] for r in rows}


# -- iCal feeds --------------------------------------------------------------

async def get_ical_feeds(enabled_only=False) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM ical_feeds"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY name"
        async with db.execute(query) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def add_ical_feed(name: str, url: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO ical_feeds (name, url) VALUES (?, ?)",
            (name, url)
        )
        await db.commit()
        return cur.lastrowid


async def toggle_ical_feed(feed_id: int, enabled: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE ical_feeds SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, feed_id)
        )
        await db.commit()


async def delete_ical_feed(feed_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM ical_feeds WHERE id = ?", (feed_id,))
        await db.commit()


# -- Upload history ----------------------------------------------------------

async def add_upload(
    display_name: str,
    start_date: str,
    end_date: str,
    event_count: int,
    pdf_path: str,
    uploaded_to_rm: bool = False,
    sync_action: str = None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO uploads
               (display_name, start_date, end_date, event_count, pdf_path,
                uploaded_to_rm, sync_action)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (display_name, start_date, end_date, event_count, pdf_path,
             1 if uploaded_to_rm else 0, sync_action)
        )
        await db.commit()
        return cur.lastrowid


async def update_upload_rm_status(upload_id: int, uploaded: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE uploads SET uploaded_to_rm = ? WHERE id = ?",
            (1 if uploaded else 0, upload_id)
        )
        await db.commit()


async def clear_uploads(delete_files: bool = False):
    async with aiosqlite.connect(DB_PATH) as db:
        if delete_files:
            async with db.execute("SELECT pdf_path FROM uploads WHERE pdf_path IS NOT NULL") as cur:
                rows = await cur.fetchall()
            for (path,) in rows:
                try:
                    if path and os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass
        await db.execute("DELETE FROM uploads")
        await db.commit()


async def get_uploads(limit=20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM uploads ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# -- Meeting slots (rolling yearly planner) ----------------------------------

async def get_meeting_slots(year: int) -> dict:
    """Return {event_id: slot} for a year's already-assigned meeting pages."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT event_id, slot FROM meeting_slots WHERE year = ?", (year,)
        ) as cur:
            rows = await cur.fetchall()
            return {r[0]: r[1] for r in rows}


async def count_meeting_slots(year: int) -> int:
    """How many page slots are claimed for `year` (reserved, never reused)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM meeting_slots WHERE year = ?", (year,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def clear_meeting_slots(year: int):
    """Drop all slot assignments for a year.

    Only safe to call when the document is being recreated (handwriting is being
    reset anyway) — e.g. the slot count or slot filter changed. Lets the next
    run re-assign slots cleanly instead of inheriting stale assignments.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM meeting_slots WHERE year = ?", (year,))
        await db.commit()


async def assign_meeting_slots(year: int, events: list, pool_size: int) -> dict:
    """
    Ensure every event has a stable page slot for `year`, returning the full
    {event_id: slot} map.

    Slots are assigned once and never moved or reused: a meeting keeps its slot
    (and therefore its on-device note page and handwriting) even if it is
    rescheduled, and a deleted meeting's slot stays reserved so its slot is
    never handed to a different meeting. New meetings take the lowest free slot.
    `events` should be pre-sorted (e.g. by start time) so first-time assignment
    is deterministic. Events beyond `pool_size` get no dedicated slot.
    """
    existing = await get_meeting_slots(year)
    used = set(existing.values())
    free = [i for i in range(pool_size) if i not in used]
    fi = 0
    to_add = []
    for e in events:
        eid = e.id
        if eid in existing:
            continue
        if fi >= len(free):
            break  # pool exhausted — meeting still shows on day/week pages
        slot = free[fi]; fi += 1
        existing[eid] = slot
        to_add.append((year, eid, slot, (e.title or "")[:200]))

    if to_add:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executemany(
                """INSERT OR IGNORE INTO meeting_slots (year, event_id, slot, title)
                   VALUES (?, ?, ?, ?)""",
                to_add,
            )
            await db.commit()
    return existing
