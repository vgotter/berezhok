import asyncio
import ipaddress
import json
import re
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
CURRENCY_ALIASES = (
    (
        "RUB",
        r"₽|(?<![A-Za-zА-Яа-яЁё])(?:rub|rur|ру|р|"
        r"руб(?:ль|ля|лей|ли)?|руб(?:ас|лик)(?:а|и|ов)?|"
        r"деревянн(?:ый|ого|ые|ых)?)\.?(?![A-Za-zА-Яа-яЁё])",
    ),
    (
        "USD",
        r"\$|(?<![A-Za-zА-Яа-яЁё])(?:usd|доллар(?:а|ы|ов)?|"
        r"бакс(?:а|ы|ов)?|бач(?:а|и|ей)?|зел[её]н(?:ый|ого|ые|ых)|"
        r"грин(?:а|ы|ов)?|у\.?\s*е\.?)"
        r"(?![A-Za-zА-Яа-яЁё])",
    ),
    (
        "EUR",
        r"€|(?<![A-Za-zА-Яа-яЁё])(?:eur|евро|еврик(?:а|и|ов)?|ойро)"
        r"(?![A-Za-zА-Яа-яЁё])",
    ),
    ("GBP", r"£|(?<![A-Za-zА-Яа-яЁё])(?:gbp|фунт(?:а|ов)?)(?![A-Za-zА-Яа-яЁё])"),
    ("GEL", r"₾|(?<![A-Za-zА-Яа-яЁё])(?:gel|лари)(?![A-Za-zА-Яа-яЁё])"),
    ("AMD", r"֏|(?<![A-Za-zА-Яа-яЁё])(?:amd|драм(?:а|ов)?)(?![A-Za-zА-Яа-яЁё])"),
    ("TRY", r"₺|(?<![A-Za-zА-Яа-яЁё])(?:try|лир(?:а|ы)?)(?![A-Za-zА-Яа-яЁё])"),
    ("ILS", r"₪|(?<![A-Za-zА-Яа-яЁё])(?:ils|шекел(?:ь|я|ей)?)(?![A-Za-zА-Яа-яЁё])"),
    ("KZT", r"₸|(?<![A-Za-zА-Яа-яЁё])(?:kzt|тенге)(?![A-Za-zА-Яа-яЁё])"),
    ("UAH", r"₴|(?<![A-Za-zА-Яа-яЁё])(?:uah|грн|грив(?:на|ны|ен))(?![A-Za-zА-Яа-яЁё])"),
    ("KGS", r"(?<![A-Za-zА-Яа-яЁё])(?:kgs|сом)(?![A-Za-zА-Яа-яЁё])"),
    ("UZS", r"(?<![A-Za-zА-Яа-яЁё])(?:uzs|сум)(?![A-Za-zА-Яа-яЁё])"),
    ("RSD", r"(?<![A-Za-zА-Яа-яЁё])(?:rsd|дин|динар(?:а|ов)?)(?![A-Za-zА-Яа-яЁё])"),
    ("AED", r"(?<![A-Za-zА-Яа-яЁё])(?:aed|дирхам(?:а|ов)?)(?![A-Za-zА-Яа-яЁё])"),
    ("BYN", r"(?<![A-Za-zА-Яа-яЁё])(?:byn|br|бел(?:орусских|орусский)?\s+руб(?:ль|ля|лей)?)(?![A-Za-zА-Яа-яЁё])"),
    ("CNY", r"¥|(?<![A-Za-zА-Яа-яЁё])(?:cny|юан(?:ь|я|ей))(?![A-Za-zА-Яа-яЁё])"),
)
MULTIPLIER_ALIASES = (
    (
        Decimal("1000000000"),
        r"(?:млрд\.?|миллиард(?:а|ы|ов)?|ярд(?:а|ы|ов)?)",
    ),
    (
        Decimal("1000000"),
        r"(?:млн\.?|миллион(?:а|ы|ов)?|лям(?:а|ы|ов)?|"
        r"лимон(?:а|ы|ов)?|мульт(?:а|ы|ов)?|кк)",
    ),
    (
        Decimal("1000"),
        r"(?:к|k|тыс\.?|тысяч(?:а|и|у)?|тыщ(?:а|и|у|ей|онка|онки|онок)?|"
        r"косар(?:ь|я|ей|ик(?:а|и|ов)?)|тонн(?:а|ы|у)?|"
        r"к[еэ]с(?:а|ы|ов)?|тыр(?:а|ы|ов)?|тырик(?:а|и|ов)?|"
        r"кос(?:ой|ого|ые|ых)|штук(?:а|и)?)",
    ),
    (
        Decimal("100"),
        r"(?:сотн(?:я|и|ю|ей)?|сот(?:ка|ки|ок|очка|очки|очек)|"
        r"сот[еэ]н(?:чик(?:а|и|ов)?)?|стольник(?:а|и|ов)?|"
        r"сотик(?:а|и|ов)?|сотыч(?:а|и|ей)?)",
    ),
    (
        Decimal("10"),
        r"(?:десятк(?:а|и|у|ок)|десяточк(?:а|и|у|ек)|"
        r"десят(?:ик|чик)(?:а|и|ов)?|"
        r"(?:дестюн|десюн|десятюн)(?:чик(?:а|и|ов)?)?|"
        r"чирик(?:а|и|ов)?|червон(?:ец|ца|цы|цев))",
    ),
)


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


def normalize_user_price(value: str, default_currency: str = "RUB") -> str:
    original = clean_text(value)[:100]
    if not original:
        return ""
    amount_text = original
    currency = ""
    for code, pattern in CURRENCY_ALIASES:
        match = re.search(pattern, amount_text, flags=re.IGNORECASE)
        if match:
            currency = code
            amount_text = (amount_text[:match.start()] + amount_text[match.end():]).strip()
            break
    for multiplier, pattern in MULTIPLIER_ALIASES:
        short = re.fullmatch(
            rf"(\d+(?:[.,]\d+)?)\s*{pattern}", amount_text, flags=re.IGNORECASE
        )
        bare = re.fullmatch(pattern, amount_text, flags=re.IGNORECASE)
        half = re.fullmatch(rf"пол\s*{pattern}", amount_text, flags=re.IGNORECASE)
        if not short and not bare and not half:
            continue
        try:
            quantity = (
                Decimal(short.group(1).replace(",", "."))
                if short
                else Decimal("0.5") if half
                else Decimal("1")
            )
            amount_text = format(
                (quantity * multiplier).normalize(),
                "f",
            )
        except InvalidOperation:
            return original
        break
    compact = amount_text.replace("\u00a0", "").replace(" ", "")
    if not re.fullmatch(r"\d+(?:[.,]\d+)?", compact):
        return original
    return format_price(compact, currency or default_currency)


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
