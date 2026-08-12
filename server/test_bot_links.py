import unittest
from types import SimpleNamespace

import os

os.environ["BOT_TOKEN"] = "123456:test-token"

from bot import message_url, shared_name_hint


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


if __name__ == "__main__":
    unittest.main()
