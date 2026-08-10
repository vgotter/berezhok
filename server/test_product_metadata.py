import unittest

from product_metadata import (
    ProductFetchError,
    ensure_public_url,
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


class ProductFetchSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def test_local_addresses_are_rejected(self):
        with self.assertRaises(ProductFetchError):
            await ensure_public_url("http://127.0.0.1/private")

    async def test_non_http_urls_are_rejected(self):
        with self.assertRaises(ProductFetchError):
            await ensure_public_url("file:///etc/passwd")


if __name__ == "__main__":
    unittest.main()
