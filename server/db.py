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
    # Безопасная миграция для уже работающего сервера: ALTER TABLE сохраняет
    # все существующие записи и лишь добавляет место для имени файла фото.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if "photo_filename" not in columns:
        try:
            conn.execute("ALTER TABLE items ADD COLUMN photo_filename TEXT")
        except sqlite3.OperationalError as exc:
            # API и бот могут впервые запуститься одновременно. Если второй
            # процесс увидел колонку уже после проверки, миграция всё равно готова.
            if "duplicate column name" not in str(exc).lower():
                raise
    for column, definition in (
        ("reason", "TEXT"),
        ("deleted_at", "INTEGER"),
    ):
        if column not in columns:
            try:
                conn.execute(f"ALTER TABLE items ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER PRIMARY KEY,
            default_wait_days REAL DEFAULT 7,
            hide_waiting INTEGER DEFAULT 0,
            archive_action TEXT DEFAULT 'archive',
            archive_after_days REAL DEFAULT 30,
            self_pronoun TEXT DEFAULT 'she',
            gentle_reminders INTEGER DEFAULT 1,
            last_seen_at INTEGER,
            last_gentle_reminder_at INTEGER
        )
        """
    )
    settings_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(settings)")
    }
    for column, definition in (
        ("self_pronoun", "TEXT DEFAULT 'she'"),
        ("gentle_reminders", "INTEGER DEFAULT 1"),
        ("last_seen_at", "INTEGER"),
        ("last_gentle_reminder_at", "INTEGER"),
    ):
        if column not in settings_columns:
            try:
                conn.execute(
                    f"ALTER TABLE settings ADD COLUMN {column} {definition}"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_links (
            user_id INTEGER PRIMARY KEY,
            url TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS link_drafts (
            user_id INTEGER PRIMARY KEY,
            draft_id TEXT NOT NULL,
            url TEXT NOT NULL,
            name TEXT NOT NULL,
            price TEXT NOT NULL DEFAULT '',
            photo_filename TEXT,
            edit_field TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    return uuid.uuid4().hex[:12]
