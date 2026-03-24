"""SQLite database setup and helpers for the Hearing Quote Finder."""

import aiosqlite
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "hearing_quotes.db")


async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    db = await get_db()
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS hearings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_id TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            date_held TEXT,
            chamber TEXT,
            committee TEXT,
            transcript_text TEXT,
            transcript_fetched INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hearing_id INTEGER NOT NULL REFERENCES hearings(id),
            topic_id INTEGER NOT NULL REFERENCES topics(id),
            speaker TEXT NOT NULL,
            quote_text TEXT NOT NULL,
            context_before TEXT DEFAULT '',
            context_after TEXT DEFAULT '',
            relevance TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS processing_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id INTEGER NOT NULL REFERENCES topics(id),
            hearing_id INTEGER NOT NULL REFERENCES hearings(id),
            status TEXT DEFAULT 'pending',
            error TEXT,
            UNIQUE(topic_id, hearing_id)
        );

        CREATE INDEX IF NOT EXISTS idx_quotes_topic ON quotes(topic_id);
        CREATE INDEX IF NOT EXISTS idx_quotes_hearing ON quotes(hearing_id);
        CREATE INDEX IF NOT EXISTS idx_processing_status ON processing_status(topic_id, status);
    """)
    await db.commit()
    await db.close()
