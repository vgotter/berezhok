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


class ProductFetchSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def test_local_addresses_are_rejected(self):
        with self.assertRaises(ProductFetchError):
            await ensure_public_url("http://127.0.0.1/private")

    async def test_non_http_urls_are_rejected(self):
        with self.assertRaises(ProductFetchError):
            await ensure_public_url("file:///etc/passwd")


if __name__ == "__main__":
    unittest.main()
