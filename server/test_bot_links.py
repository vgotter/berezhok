import os
import sqlite3
import unittest
from types import SimpleNamespace

os.environ["BOT_TOKEN"] = "123456:test-token"

from bot import (
    mark_reminder_undeliverable,
    message_url,
    parse_draft_details,
    shared_name_hint,
)


class LinkMessageTest(unittest.TestCase):
    def message(self, text="", caption="", entities=None, caption_entities=None):
        return SimpleNamespace(
            text=text or None,
            caption=caption or None,
            entities=entities or [],
            caption_entities=caption_entities or [],
        )

    def test_plain_url_is_found(self):
        message = self.message(text="Смотри https://shop.example/item).")
        self.assertEqual(message_url(message), "https://shop.example/item")

    def test_url_in_caption_is_found(self):
        message = self.message(caption="Кресло www.shop.example/chair")
        self.assertEqual(message_url(message), "https://www.shop.example/chair")

    def test_hidden_telegram_link_is_found(self):
        entity = SimpleNamespace(type="text_link", url="https://shop.example/item")
        message = self.message(text="Открыть товар", entities=[entity])
        self.assertEqual(message_url(message), "https://shop.example/item")

    def test_caption_can_be_used_as_name_hint(self):
        text = "Кресло для чтения https://shop.example/item"
        self.assertEqual(
            shared_name_hint(text, "https://shop.example/item"),
            "Кресло для чтения",
        )

    def test_name_and_price_can_be_sent_in_one_message(self):
        self.assertEqual(
            parse_draft_details("Беговая дорожка\n10 косарей"),
            ("Беговая дорожка", "10000 ₽"),
        )
        self.assertEqual(
            parse_draft_details("Кресло — 300 баксов"),
            ("Кресло", "$300"),
        )
        self.assertEqual(
            parse_draft_details("Утятница\n5000", "AMD"),
            ("Утятница", "5000 ֏"),
        )

    def test_name_and_price_need_a_separator(self):
        self.assertIsNone(parse_draft_details("Только название"))

    def test_blocked_recipient_stops_only_that_reminder(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE items (id TEXT PRIMARY KEY, notified INTEGER)")
        conn.executemany(
            "INSERT INTO items (id, notified) VALUES (?, 0)",
            (("blocked",), ("another",)),
        )

        mark_reminder_undeliverable(conn, "blocked")

        rows = dict(conn.execute("SELECT id, notified FROM items"))
        conn.close()
        self.assertEqual(rows, {"blocked": 1, "another": 0})


if __name__ == "__main__":
    unittest.main()
