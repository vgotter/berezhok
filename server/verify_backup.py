import argparse
import io
import sqlite3
import tarfile
import tempfile
from pathlib import Path

from PIL import Image, UnidentifiedImageError


def verify_backup(archive_path: Path) -> dict:
    if not archive_path.is_file():
        raise ValueError(f"backup not found: {archive_path}")

    photo_count = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        members = {member.name: member for member in archive.getmembers()}
        database_member = members.get("berezhok.db")
        if not database_member or not database_member.isfile():
            raise ValueError("backup does not contain berezhok.db")

        database_file = archive.extractfile(database_member)
        if database_file is None:
            raise ValueError("cannot read berezhok.db from backup")

        with tempfile.TemporaryDirectory(prefix="berezhok-verify-") as temp_name:
            database_path = Path(temp_name) / "berezhok.db"
            database_path.write_bytes(database_file.read())
            conn = sqlite3.connect(str(database_path))
            try:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise ValueError(f"database integrity check failed: {integrity}")
                item_count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            finally:
                conn.close()

        for name, member in members.items():
            if not member.isfile() or not name.startswith("uploads/"):
                continue
            photo_file = archive.extractfile(member)
            if photo_file is None:
                raise ValueError(f"cannot read {name}")
            try:
                with Image.open(io.BytesIO(photo_file.read())) as image:
                    image.verify()
            except (UnidentifiedImageError, OSError) as error:
                raise ValueError(f"damaged photo in backup: {name}") from error
            photo_count += 1

    return {"items": item_count, "photos": photo_count}


def main():
    parser = argparse.ArgumentParser(description="Verify a Berezhok backup archive")
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    result = verify_backup(args.archive.resolve())
    print(
        f"Backup is healthy: {result['items']} items, "
        f"{result['photos']} photos"
    )


if __name__ == "__main__":
    main()
