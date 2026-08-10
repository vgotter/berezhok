import asyncio
import ipaddress
import json
import socket
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup


MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
)
CURRENCY_SYMBOLS = {
    "RUB": "₽", "USD": "$", "EUR": "€", "GBP": "£", "GEL": "₾",
    "AMD": "֏", "TRY": "₺", "ILS": "₪", "KZT": "₸", "UAH": "₴",
    "KGS": "сом", "UZS": "сум", "RSD": "дин", "AED": "AED",
    "BYN": "Br", "CNY": "¥",
}


class ProductFetchError(Exception):
    pass


@dataclass
class ProductMetadata:
    name: str = ""
    price: str = ""
    image_url: str = ""


def clean_text(value) -> str:
    return " ".join(str(value or "").split()).strip()


def first_value(value):
    if isinstance(value, list):
        return first_value(value[0]) if value else ""
    if isinstance(value, dict):
        return value.get("url") or value.get("contentUrl") or ""
    return value or ""


def iter_json_nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_json_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_json_nodes(child)


def is_product_node(node) -> bool:
    node_type = node.get("@type") if isinstance(node, dict) else None
    types = node_type if isinstance(node_type, list) else [node_type]
    return any(str(value or "").lower().endswith("product") for value in types)


def offer_price(offers):
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        return "", ""
    price = offers.get("price") or offers.get("lowPrice")
    currency = offers.get("priceCurrency")
    if price is None and isinstance(offers.get("priceSpecification"), dict):
        specification = offers["priceSpecification"]
        price = specification.get("price")
        currency = currency or specification.get("priceCurrency")
    return clean_text(price), clean_text(currency).upper()


def format_price(amount: str, currency: str) -> str:
    amount = clean_text(amount)
    if not amount:
        return ""
    normalized = amount.replace("\u00a0", " ").replace(" ", "").replace(",", ".")
    try:
        number = Decimal(normalized)
        if number == number.to_integral():
            amount = str(number.quantize(Decimal("1")))
        else:
            amount = format(number.normalize(), "f")
    except InvalidOperation:
        pass
    symbol = CURRENCY_SYMBOLS.get(currency.upper(), currency.upper()) if currency else ""
    if not symbol:
        return amount
    return f"{symbol}{amount}" if symbol in {"$", "£"} else f"{amount} {symbol}"


def meta_content(soup, *keys) -> str:
    for key in keys:
        tag = soup.find("meta", attrs={"property": key}) or soup.find(
            "meta", attrs={"name": key}
        )
        if tag and tag.get("content"):
            return clean_text(tag["content"])
    return ""


def parse_product_html(html, base_url: str) -> ProductMetadata:
    soup = BeautifulSoup(html, "html.parser")
    metadata = ProductMetadata()

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        product = next((node for node in iter_json_nodes(payload) if is_product_node(node)), None)
        if not product:
            continue
        metadata.name = clean_text(product.get("name"))
        amount, currency = offer_price(product.get("offers"))
        metadata.price = format_price(amount, currency)
        metadata.image_url = clean_text(first_value(product.get("image")))
        break

    metadata.name = metadata.name or meta_content(soup, "og:title", "twitter:title")
    metadata.image_url = metadata.image_url or meta_content(
        soup, "og:image", "og:image:secure_url", "twitter:image"
    )
    if not metadata.price:
        amount = meta_content(
            soup, "product:price:amount", "og:price:amount", "twitter:data1"
        )
        currency = meta_content(
            soup, "product:price:currency", "og:price:currency"
        )
        metadata.price = format_price(amount, currency)
    if not metadata.name and soup.title:
        metadata.name = clean_text(soup.title.get_text())
    if metadata.image_url:
        metadata.image_url = urljoin(base_url, metadata.image_url)
    return metadata


async def ensure_public_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProductFetchError("unsupported url")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ProductFetchError("invalid port") from exc
    try:
        results = await asyncio.get_running_loop().run_in_executor(
            None, lambda: socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        )
    except socket.gaierror as exc:
        raise ProductFetchError("host not found") from exc
    addresses = {row[4][0] for row in results}
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ProductFetchError("private address is not allowed")
    return url


async def fetch_bytes(url: str, max_bytes: int, expected: Optional[str] = None):
    timeout = aiohttp.ClientTimeout(total=12, connect=5, sock_read=7)
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"}
    current = url
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for _ in range(6):
            await ensure_public_url(current)
            async with session.get(current, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise ProductFetchError("redirect without location")
                    current = urljoin(current, location)
                    continue
                if response.status != 200:
                    raise ProductFetchError(f"http {response.status}")
                content_type = response.headers.get("Content-Type", "").lower()
                if expected and expected not in content_type:
                    raise ProductFetchError("unexpected content type")
                declared = response.content_length
                if declared is not None and declared > max_bytes:
                    raise ProductFetchError("response is too large")
                chunks = []
                size = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ProductFetchError("response is too large")
                    chunks.append(chunk)
                return current, b"".join(chunks), content_type
    raise ProductFetchError("too many redirects")


async def fetch_product_metadata(url: str) -> ProductMetadata:
    final_url, body, _ = await fetch_bytes(url, MAX_PAGE_BYTES, "text/html")
    return parse_product_html(body, final_url)


async def fetch_product_image(url: str) -> bytes:
    _, body, _ = await fetch_bytes(url, MAX_IMAGE_BYTES, "image/")
    return body
