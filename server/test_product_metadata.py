import unittest

from product_metadata import (
    ProductFetchError,
    ensure_public_url,
    normalize_user_price,
    parse_product_html,
)


class ProductMetadataTest(unittest.TestCase):
    def test_json_ld_product_has_priority(self):
        html = """
        <html><head>
        <meta property="og:title" content="Запасное название">
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Product",
          "name": "Беговая дорожка",
          "image": "/images/track.jpg",
          "offers": {"@type": "Offer", "price": "10000", "priceCurrency": "RUB"}
        }
        </script></head></html>
        """
        result = parse_product_html(html, "https://shop.example/catalog/item")
        self.assertEqual(result.name, "Беговая дорожка")
        self.assertEqual(result.price, "10000 ₽")
        self.assertEqual(result.image_url, "https://shop.example/images/track.jpg")

    def test_open_graph_is_used_as_fallback(self):
        html = """
        <html><head>
        <meta property="og:title" content="  Красивый   чайник ">
        <meta property="og:image" content="https://cdn.example/kettle.png">
        <meta property="product:price:amount" content="25.50">
        <meta property="product:price:currency" content="USD">
        </head></html>
        """
        result = parse_product_html(html, "https://shop.example/item")
        self.assertEqual(result.name, "Красивый чайник")
        self.assertEqual(result.price, "$25.5")
        self.assertEqual(result.image_url, "https://cdn.example/kettle.png")

    def test_human_price_input_is_normalized(self):
        ruble_examples = {
            "5000": "5000 ₽",
            "5000 руб": "5000 ₽",
            "5000 рубля": "5000 ₽",
            "5000 рублей": "5000 ₽",
            "5000 рубли": "5000 ₽",
            "5000 RUB": "5000 ₽",
            "5000р": "5000 ₽",
            "5к": "5000 ₽",
            "10 тыс.": "10000 ₽",
        }
        for value, expected in ruble_examples.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_user_price(value), expected)
        self.assertEqual(normalize_user_price("250 usd"), "$250")
        self.assertEqual(normalize_user_price("20 евро"), "20 €")
        self.assertEqual(normalize_user_price("50 лари"), "50 ₾")
        self.assertEqual(
            normalize_user_price("цена по запросу"), "цена по запросу"
        )

    def test_price_slang_is_normalized(self):
        examples = {
            "300 рубасов": "300 ₽",
            "300 рубликов": "300 ₽",
            "300 ру": "300 ₽",
            "300 баксов": "$300",
            "300 бачей": "$300",
            "300 зеленых": "$300",
            "300 зелёных": "$300",
            "300 евриков": "300 €",
            "5 тыщ": "5000 ₽",
            "5 тыщонок": "5000 ₽",
            "1 косарь": "1000 ₽",
            "2 косаря": "2000 ₽",
            "5 косарей": "5000 ₽",
            "3 косарика": "3000 ₽",
            "2 тонны": "2000 ₽",
            "4 кэс": "4000 ₽",
            "4 кеса": "4000 ₽",
            "1.5 ляма": "1500000 ₽",
            "2 лимона": "2000000 ₽",
            "3 кк": "3000000 ₽",
            "2 мульта": "2000000 ₽",
            "1 ярд": "1000000000 ₽",
            "5 косарей баксов": "$5000",
            "косарь": "1000 ₽",
            "лям": "1000000 ₽",
            "полляма": "500000 ₽",
            "косарь баксов": "$1000",
        }
        for value, expected in examples.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_user_price(value), expected)


class ProductFetchSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def test_local_addresses_are_rejected(self):
        with self.assertRaises(ProductFetchError):
            await ensure_public_url("http://127.0.0.1/private")

    async def test_non_http_urls_are_rejected(self):
        with self.assertRaises(ProductFetchError):
            await ensure_public_url("file:///etc/passwd")


if __name__ == "__main__":
    unittest.main()
