import os
import sqlite3
import time
import uuid

DB_PATH = os.environ.get("DB_PATH", "berezhok.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            url TEXT,
            price TEXT,
            wait_days REAL,
            added_at INTEGER NOT NULL,
            decision TEXT,
            decided_at INTEGER,
            archived INTEGER DEFAULT 0,
            notified INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER PRIMARY KEY,
            default_wait_days REAL DEFAULT 7,
            hide_waiting INTEGER DEFAULT 0,
            archive_action TEXT DEFAULT 'archive',
            archive_after_days REAL DEFAULT 30
        )
        """
    )
    conn.commit()
    conn.close()


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    return uuid.uuid4().hex[:12]
