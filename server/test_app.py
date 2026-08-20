import hashlib
import hmac
import io
import json
import os
import sqlite3
import sys
import tempfile
import tarfile
import time
import unittest
import urllib.parse
import zipfile
from datetime import datetime, timezone

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
    """
    CREATE TABLE settings (
        user_id INTEGER PRIMARY KEY,
        default_wait_days REAL DEFAULT 7,
        hide_waiting INTEGER DEFAULT 0,
        archive_action TEXT DEFAULT 'archive',
        archive_after_days REAL DEFAULT 30
    )
    """
)
legacy.execute(
    "INSERT INTO settings (user_id, default_wait_days) VALUES (?,?)", (101, 14)
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
import backup  # noqa: E402
import analytics  # noqa: E402
import gentle_reminders  # noqa: E402
import verify_backup  # noqa: E402


def auth_headers(user_id, auth_date=None, username=None):
    user = {"id": user_id}
    if username:
        user["username"] = username
    fields = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": f"query-{user_id}",
        "user": json.dumps(user, separators=(",", ":")),
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
        settings_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(settings)")
        }
        old_item = conn.execute(
            "SELECT name FROM items WHERE id='old-item'"
        ).fetchone()
        old_settings = conn.execute(
            "SELECT default_wait_days, self_pronoun FROM settings WHERE user_id=101"
        ).fetchone()
        draft_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='link_drafts'"
        ).fetchone()
        conn.close()
        self.assertIn("photo_filename", columns)
        self.assertIn("reason", columns)
        self.assertIn("deleted_at", columns)
        self.assertIn("need_test_result", columns)
        self.assertIn("need_test_answers", columns)
        self.assertIn("need_test_completed_at", columns)
        self.assertIn("self_pronoun", settings_columns)
        self.assertIn("gentle_reminders", settings_columns)
        self.assertIn("last_seen_at", settings_columns)
        self.assertIn("last_gentle_reminder_at", settings_columns)
        self.assertEqual(old_item[0], "Старая вещь")
        self.assertEqual(old_settings, (14.0, "she"))
        self.assertEqual(draft_table[0], "link_drafts")
        conn = sqlite3.connect(DB_PATH)
        config_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='app_config'"
        ).fetchone()
        analytics_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('analytics_users', 'analytics_daily')"
            )
        }
        conn.close()
        self.assertEqual(config_table[0], "app_config")
        self.assertEqual(analytics_tables, {"analytics_users", "analytics_daily"})

    def test_01b_test_waits_are_visible_only_to_owner(self):
        regular = self.client.get(
            "/api/state", headers=auth_headers(707, username="someone_else")
        )
        owner = self.client.get(
            "/api/state", headers=auth_headers(708, username="IronOtter")
        )
        self.assertFalse(regular.json()["features"]["testWaits"])
        self.assertTrue(owner.json()["features"]["testWaits"])

    def test_02_existing_json_endpoint_still_works(self):
        response = self.client.post(
            "/api/items",
            headers=auth_headers(101),
            json={
                "name": "Без фотографии",
                "price": "100 ₽",
                "reason": "Нужна для прогулок",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("id", response.json())

    def test_03_photo_is_resized_and_only_owner_can_read_it(self):
        source = io.BytesIO()
        Image.new("RGB", (2400, 1200), (140, 90, 107)).save(source, "PNG")
        response = self.client.post(
            "/api/items-with-photo",
            headers=auth_headers(101),
            data={
                "name": "Вещь с фотографией",
                "reason": "Хочу проверить фотографию",
                "waitDays": "7",
            },
            files={"photo": ("source.png", source.getvalue(), "image/png")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["hasPhoto"])
        item_id = response.json()["id"]
        self.__class__.photo_item_id = item_id

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

    def test_05_item_can_be_edited_and_photo_removed(self):
        item_id = self.photo_item_id
        response = self.client.put(
            f"/api/items/{item_id}",
            headers=auth_headers(101),
            data={
                "name": "Исправленное название",
                "url": "https://example.test/item",
                "price": "$25",
                "reason": "Новая причина",
                "waitDays": "default",
                "removePhoto": "true",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["hasPhoto"])
        missing_photo = self.client.get(
            f"/api/items/{item_id}/photo", headers=auth_headers(101)
        )
        self.assertEqual(missing_photo.status_code, 404)
        conn = sqlite3.connect(DB_PATH)
        item = conn.execute(
            "SELECT name, price, reason, wait_days, photo_filename FROM items WHERE id=?",
            (item_id,),
        ).fetchone()
        conn.close()
        self.assertEqual(
            item, ("Исправленное название", "$25", "Новая причина", None, None)
        )

    def test_06_snooze_reopens_item_and_resets_notification(self):
        item_id = self.photo_item_id
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE items SET archived=1, decision='drop', notified=1 WHERE id=?",
            (item_id,),
        )
        conn.commit()
        conn.close()
        response = self.client.post(
            f"/api/items/{item_id}/snooze",
            headers=auth_headers(101),
            json={"days": 3},
        )
        self.assertEqual(response.status_code, 200)
        conn = sqlite3.connect(DB_PATH)
        item = conn.execute(
            "SELECT wait_days, archived, decision, notified FROM items WHERE id=?",
            (item_id,),
        ).fetchone()
        conn.close()
        self.assertEqual(item, (3.0, 0, None, 0))

    def test_07_bought_decision_and_pronoun_are_saved(self):
        item_id = self.photo_item_id
        decision = self.client.post(
            f"/api/items/{item_id}/decide",
            headers=auth_headers(101),
            json={"decision": "bought"},
        )
        self.assertEqual(decision.status_code, 200)
        settings = self.client.put(
            "/api/settings",
            headers=auth_headers(101),
            json={"selfPronoun": "he"},
        )
        self.assertEqual(settings.status_code, 200)
        state = self.client.get("/api/state", headers=auth_headers(101)).json()
        saved = next(item for item in state["items"] if item["id"] == item_id)
        self.assertEqual(saved["decision"], "bought")
        self.assertEqual(state["settings"]["selfPronoun"], "he")

    def test_08_delete_is_owner_scoped(self):
        created = self.client.post(
            "/api/items",
            headers=auth_headers(101),
            json={"name": "Удаляемая вещь"},
        ).json()["id"]
        denied = self.client.delete(
            f"/api/items/{created}", headers=auth_headers(202)
        )
        self.assertEqual(denied.status_code, 404)
        deleted = self.client.delete(
            f"/api/items/{created}", headers=auth_headers(101)
        )
        self.assertEqual(deleted.status_code, 200)
        state = self.client.get("/api/state", headers=auth_headers(101)).json()
        self.assertNotIn(created, {item["id"] for item in state["items"]})
        restored = self.client.post(
            f"/api/items/{created}/undo", headers=auth_headers(101)
        )
        self.assertEqual(restored.status_code, 200)
        state = self.client.get("/api/state", headers=auth_headers(101)).json()
        self.assertIn(created, {item["id"] for item in state["items"]})

    def test_09_archived_item_can_be_returned_to_waiting(self):
        item_id = self.photo_item_id
        restored = self.client.post(
            f"/api/items/{item_id}/restore", headers=auth_headers(101)
        )
        self.assertEqual(restored.status_code, 200)
        state = self.client.get("/api/state", headers=auth_headers(101)).json()
        item = next(item for item in state["items"] if item["id"] == item_id)
        self.assertFalse(item["archived"])
        self.assertIsNone(item["decision"])

    def test_09b_wishlist_item_can_be_checked_as_bought(self):
        item_id = self.photo_item_id
        kept = self.client.post(
            f"/api/items/{item_id}/decide",
            headers=auth_headers(101),
            json={"decision": "keep"},
        )
        bought = self.client.post(
            f"/api/items/{item_id}/decide",
            headers=auth_headers(101),
            json={"decision": "bought"},
        )
        self.assertEqual(kept.status_code, 200)
        self.assertEqual(bought.status_code, 200)
        state = self.client.get("/api/state", headers=auth_headers(101)).json()
        item = next(item for item in state["items"] if item["id"] == item_id)
        self.assertEqual(item["decision"], "bought")
        self.assertTrue(item["archived"])

    def test_10_pending_link_is_consumed_once(self):
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO pending_links (user_id, url, created_at) VALUES (?,?,?)",
            (101, "https://shop.example/item", 1),
        )
        conn.commit()
        conn.close()
        first = self.client.post(
            "/api/pending-link/consume", headers=auth_headers(101)
        )
        second = self.client.post(
            "/api/pending-link/consume", headers=auth_headers(101)
        )
        self.assertEqual(first.json()["url"], "https://shop.example/item")
        self.assertEqual(second.json()["url"], "")

    def test_11_five_minute_wait_is_supported(self):
        five_minutes = 5 / (24 * 60)
        created = self.client.post(
            "/api/items",
            headers=auth_headers(101),
            json={"name": "Пятиминутный тест", "waitDays": five_minutes},
        )
        self.assertEqual(created.status_code, 200)
        state = self.client.get("/api/state", headers=auth_headers(101)).json()
        item = next(
            item for item in state["items"] if item["id"] == created.json()["id"]
        )
        self.assertAlmostEqual(item["waitDays"], five_minutes)

    def test_12_backup_contains_database_and_photos(self):
        os.makedirs(PHOTO_DIR, exist_ok=True)
        Image.new("RGB", (30, 30), (88, 112, 95)).save(
            os.path.join(PHOTO_DIR, "sample.jpg"), "JPEG"
        )
        created = backup.create_backup(
            datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        )
        self.assertTrue(created.is_file())
        with tarfile.open(created, "r:gz") as archive:
            names = archive.getnames()
        self.assertIn("berezhok.db", names)
        self.assertIn("uploads/sample.jpg", names)
        verified = verify_backup.verify_backup(created)
        self.assertGreaterEqual(verified["items"], 1)
        self.assertGreaterEqual(verified["photos"], 1)

    def test_13_expired_telegram_authorization_is_rejected(self):
        response = self.client.get(
            "/api/state",
            headers=auth_headers(101, int(time.time()) - 90000),
        )
        self.assertEqual(response.status_code, 401)

    def test_14_invalid_settings_are_rejected(self):
        bad_action = self.client.put(
            "/api/settings",
            headers=auth_headers(101),
            json={"archiveAction": "erase-everything"},
        )
        bad_time = self.client.put(
            "/api/settings",
            headers=auth_headers(101),
            json={"defaultWaitDays": -1},
        )
        self.assertEqual(bad_action.status_code, 422)
        self.assertEqual(bad_time.status_code, 422)

    def test_15_account_export_contains_json_and_photos(self):
        source = io.BytesIO()
        Image.new("RGB", (100, 100), (88, 112, 95)).save(source, "PNG")
        created = self.client.post(
            "/api/items-with-photo",
            headers=auth_headers(303),
            data={"name": "Экспортируемая вещь"},
            files={"photo": ("source.png", source.getvalue(), "image/png")},
        ).json()
        response = self.client.get(
            "/api/account/export", headers=auth_headers(303)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = archive.namelist()
            payload = json.loads(archive.read("berezhok-data.json"))
        self.assertIn(f"photos/{created['id']}.jpg", names)
        self.assertEqual(payload["telegramUserId"], 303)
        self.assertEqual(payload["items"][0]["name"], "Экспортируемая вещь")

    def test_16_account_deletion_requires_confirmation_and_removes_everything(self):
        denied = self.client.post(
            "/api/account/delete",
            headers=auth_headers(303),
            json={"confirmation": "удалить"},
        )
        self.assertEqual(denied.status_code, 422)
        deleted = self.client.post(
            "/api/account/delete",
            headers=auth_headers(303),
            json={"confirmation": "УДАЛИТЬ"},
        )
        self.assertEqual(deleted.status_code, 200)
        conn = sqlite3.connect(DB_PATH)
        counts = [
            conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=303").fetchone()[0]
            for table in (
                "items", "settings", "pending_links", "link_drafts",
                "analytics_daily", "analytics_users",
            )
        ]
        conn.close()
        self.assertEqual(counts, [0, 0, 0, 0, 0, 0])

    def test_16b_analytics_counts_actions_without_card_contents(self):
        user_id = 808
        first = self.client.get("/api/state", headers=auth_headers(user_id))
        second = self.client.get("/api/state", headers=auth_headers(user_id))
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        valid = self.client.post(
            "/api/analytics",
            headers=auth_headers(user_id),
            json={"event": "sos_open"},
        )
        repeated = self.client.post(
            "/api/analytics",
            headers=auth_headers(user_id),
            json={"event": "sos_open"},
        )
        invalid = self.client.post(
            "/api/analytics",
            headers=auth_headers(user_id),
            json={"event": "card_name"},
        )
        created = self.client.post(
            "/api/items",
            headers=auth_headers(user_id),
            json={"name": "Секретное название", "price": "12345 ₽"},
        )
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(created.status_code, 200)

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        snapshot = analytics.stats_snapshot(conn, int(time.time() * 1000))
        events = conn.execute(
            "SELECT event, count FROM analytics_daily WHERE user_id=?",
            (user_id,),
        ).fetchall()
        conn.close()
        self.assertEqual(snapshot["sos_today"], 1)
        self.assertGreaterEqual(snapshot["items_today"], 1)
        self.assertEqual(dict(events)["app_open"], 1)
        self.assertEqual(dict(events)["sos_open"], 1)
        self.assertNotIn("card_name", dict(events))

    def test_17_health_checks_database_bot_backup_and_disk(self):
        now = str(int(time.time() * 1000))
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO app_config (key, value) VALUES (?,?)",
            ("bot_heartbeat_ms", now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO app_config (key, value) VALUES (?,?)",
            ("last_backup_ms", now),
        )
        conn.commit()
        conn.close()
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"])

    def test_18_gentle_reminders_follow_inactivity_and_user_setting(self):
        state = self.client.get("/api/state", headers=auth_headers(404))
        self.assertEqual(state.status_code, 200)
        self.assertTrue(state.json()["settings"]["gentleReminders"])

        old_seen = int(time.time() * 1000) - 8 * gentle_reminders.DAY
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "UPDATE settings SET last_seen_at=?, last_gentle_reminder_at=NULL "
            "WHERE user_id=404",
            (old_seen,),
        )
        conn.commit()
        due = gentle_reminders.reminder_candidates(conn, int(time.time() * 1000))
        self.assertIn(404, {row["user_id"] for row in due})
        conn.close()

        disabled = self.client.put(
            "/api/settings",
            headers=auth_headers(404),
            json={"gentleReminders": False},
        )
        self.assertEqual(disabled.status_code, 200)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        due = gentle_reminders.reminder_candidates(conn, int(time.time() * 1000))
        self.assertNotIn(404, {row["user_id"] for row in due})
        conn.close()

        enabled = self.client.put(
            "/api/settings",
            headers=auth_headers(404),
            json={"gentleReminders": True},
        )
        self.assertEqual(enabled.status_code, 200)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT gentle_reminders, last_gentle_reminder_at "
            "FROM settings WHERE user_id=404"
        ).fetchone()
        conn.close()
        self.assertEqual(row["gentle_reminders"], 1)
        self.assertIsNotNone(row["last_gentle_reminder_at"])

    def test_19_need_test_is_scored_saved_and_owner_scoped(self):
        item_id = self.client.post(
            "/api/items",
            headers=auth_headers(505),
            json={"name": "Тестируемая вещь"},
        ).json()["id"]
        invalid = self.client.post(
            f"/api/items/{item_id}/need-test",
            headers=auth_headers(505),
            json={"answers": [2, 2]},
        )
        denied = self.client.post(
            f"/api/items/{item_id}/need-test",
            headers=auth_headers(606),
            json={"answers": [2, 2, 2, 2, 2, 2, 2]},
        )
        saved = self.client.post(
            f"/api/items/{item_id}/need-test",
            headers=auth_headers(505),
            json={"answers": [2, 2, 2, 2, 2, 2, 2]},
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["result"], "needed")
        not_needed = self.client.post(
            f"/api/items/{item_id}/need-test",
            headers=auth_headers(505),
            json={"answers": [0, 0, 0, 0, 0, 0, 0]},
        )
        unclear = self.client.post(
            f"/api/items/{item_id}/need-test",
            headers=auth_headers(505),
            json={"answers": [1, 1, 1, 1, 1, 1, 1]},
        )
        self.assertEqual(not_needed.json()["result"], "not_needed")
        self.assertEqual(unclear.json()["result"], "unclear")
        state = self.client.get("/api/state", headers=auth_headers(505)).json()
        item = next(entry for entry in state["items"] if entry["id"] == item_id)
        self.assertEqual(item["needTestResult"], "unclear")
        self.assertIsNotNone(item["needTestCompletedAt"])


if __name__ == "__main__":
    unittest.main()
