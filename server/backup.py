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
    return target


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
