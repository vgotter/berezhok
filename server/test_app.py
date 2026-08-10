import hashlib
import hmac
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.parse

from fastapi.testclient import TestClient
from PIL import Image


TEST_DIR = tempfile.TemporaryDirectory()
DB_PATH = os.path.join(TEST_DIR.name, "legacy.db")
PHOTO_DIR = os.path.join(TEST_DIR.name, "uploads")
BOT_TOKEN = "test-token"

# Имитируем базу с сервера до появления поддержки фотографий.
legacy = sqlite3.connect(DB_PATH)
legacy.execute(
    """
    CREATE TABLE items (
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
legacy.execute(
    "INSERT INTO items (id, user_id, name, added_at) VALUES (?,?,?,?)",
    ("old-item", 101, "Старая вещь", 1),
)
legacy.commit()
legacy.close()

os.environ["DB_PATH"] = DB_PATH
os.environ["PHOTO_DIR"] = PHOTO_DIR
os.environ["BOT_TOKEN"] = BOT_TOKEN
os.environ["ALLOWED_ORIGIN"] = "https://example.test"
sys.path.insert(0, os.path.dirname(__file__))

from app import app  # noqa: E402


def auth_headers(user_id):
    fields = {
        "auth_date": "1786310000",
        "query_id": f"query-{user_id}",
        "user": json.dumps({"id": user_id}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urllib.parse.urlencode(fields)}


class PhotoApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_01_legacy_database_is_migrated_without_data_loss(self):
        conn = sqlite3.connect(DB_PATH)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
        old_item = conn.execute(
            "SELECT name FROM items WHERE id='old-item'"
        ).fetchone()
        conn.close()
        self.assertIn("photo_filename", columns)
        self.assertEqual(old_item[0], "Старая вещь")

    def test_02_existing_json_endpoint_still_works(self):
        response = self.client.post(
            "/api/items",
            headers=auth_headers(101),
            json={"name": "Без фотографии", "price": "100 ₽"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("id", response.json())

    def test_03_photo_is_resized_and_only_owner_can_read_it(self):
        source = io.BytesIO()
        Image.new("RGB", (2400, 1200), (140, 90, 107)).save(source, "PNG")
        response = self.client.post(
            "/api/items-with-photo",
            headers=auth_headers(101),
            data={"name": "Вещь с фотографией", "waitDays": "7"},
            files={"photo": ("source.png", source.getvalue(), "image/png")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["hasPhoto"])
        item_id = response.json()["id"]

        photo = self.client.get(
            f"/api/items/{item_id}/photo", headers=auth_headers(101)
        )
        self.assertEqual(photo.status_code, 200)
        self.assertEqual(photo.headers["content-type"], "image/jpeg")
        with Image.open(io.BytesIO(photo.content)) as saved:
            self.assertEqual(saved.format, "JPEG")
            self.assertLessEqual(max(saved.size), 1600)

        other_user = self.client.get(
            f"/api/items/{item_id}/photo", headers=auth_headers(202)
        )
        self.assertEqual(other_user.status_code, 404)

    def test_04_non_image_is_rejected_without_creating_item(self):
        conn = sqlite3.connect(DB_PATH)
        before = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        conn.close()
        response = self.client.post(
            "/api/items-with-photo",
            headers=auth_headers(101),
            data={"name": "Плохой файл"},
            files={"photo": ("note.txt", b"not an image", "text/plain")},
        )
        self.assertEqual(response.status_code, 415)
        conn = sqlite3.connect(DB_PATH)
        after = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        conn.close()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
