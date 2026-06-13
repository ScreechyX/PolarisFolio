"""
Database layer. SQLite via aiosqlite.

Tables:
  settings     - key/value app config (MS client ID, RM token, etc.)
  ical_feeds   - saved ICS feed URLs
  uploads      - history of generated planners
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
                created_at   TEXT DEFAULT (datetime('now'))
            );
        """)
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
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO uploads
               (display_name, start_date, end_date, event_count, pdf_path, uploaded_to_rm)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (display_name, start_date, end_date, event_count, pdf_path, 1 if uploaded_to_rm else 0)
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
