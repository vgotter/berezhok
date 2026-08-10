import os
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


DB_PATH = Path(os.environ.get("DB_PATH", "berezhok.db")).resolve()
PHOTO_DIR = Path(
    os.environ.get("PHOTO_DIR", str(DB_PATH.parent / "uploads"))
).resolve()
BACKUP_DIR = Path(
    os.environ.get("BACKUP_DIR", str(DB_PATH.parent / "backups"))
).resolve()
BACKUP_KEEP_DAYS = int(os.environ.get("BACKUP_KEEP_DAYS", "30"))
S3_BUCKET = os.environ.get("S3_BUCKET", "").strip()
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "https://s3.twcstorage.ru").strip()
S3_REGION = os.environ.get("S3_REGION", "ru-1").strip()
S3_PREFIX = os.environ.get("S3_PREFIX", "berezhok").strip().strip("/")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "").strip()
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "").strip()


def create_backup(now=None):
    now = now or datetime.now(timezone.utc)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d_%H-%M-%S_utc")
    target = BACKUP_DIR / f"berezhok-{stamp}.tar.gz"
    temporary_archive = BACKUP_DIR / f".{target.name}.tmp"

    with tempfile.TemporaryDirectory(prefix="berezhok-backup-") as temp_name:
        database_copy = Path(temp_name) / "berezhok.db"
        source = sqlite3.connect(str(DB_PATH))
        destination = sqlite3.connect(str(database_copy))
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()

        try:
            with tarfile.open(temporary_archive, "w:gz") as archive:
                archive.add(database_copy, arcname="berezhok.db")
                if PHOTO_DIR.is_dir():
                    archive.add(PHOTO_DIR, arcname="uploads")
            os.replace(temporary_archive, target)
        finally:
            try:
                temporary_archive.unlink()
            except FileNotFoundError:
                pass

    remove_expired_backups(now)
    record_backup_success(now)
    if S3_BUCKET:
        upload_offsite(target, now)
    return target


def record_backup_success(now):
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS app_config "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO app_config (key, value) VALUES ('last_backup_ms', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(int(now.timestamp() * 1000)),),
        )
        conn.commit()
    finally:
        conn.close()


def upload_offsite(target, now):
    if not S3_ACCESS_KEY or not S3_SECRET_KEY:
        raise RuntimeError("S3 credentials are missing")
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        region_name=S3_REGION,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    key = f"{S3_PREFIX}/{target.name}" if S3_PREFIX else target.name
    client.upload_file(str(target), S3_BUCKET, key)
    record_offsite_backup_success(now)


def record_offsite_backup_success(now):
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT INTO app_config (key, value) VALUES ('last_offsite_backup_ms', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(int(now.timestamp() * 1000)),),
        )
        conn.commit()
    finally:
        conn.close()


def remove_expired_backups(now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=BACKUP_KEEP_DAYS)
    for path in BACKUP_DIR.glob("berezhok-*.tar.gz"):
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if modified < cutoff:
            path.unlink()


if __name__ == "__main__":
    created = create_backup()
    print(f"Backup created: {created} ({created.stat().st_size} bytes)")
