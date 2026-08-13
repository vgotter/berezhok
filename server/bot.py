import asyncio
import io
import logging
import os
import re
import time
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from aiogram.filters import Command, CommandStart
from PIL import Image, ImageOps, UnidentifiedImageError

from db import DB_PATH, get_conn, init_db, new_id, now_ms
from gentle_reminders import reminder_candidates, reminder_text
from product_metadata import (
    ProductFetchError,
    fetch_product_image,
    fetch_product_metadata,
    normalize_user_price,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://vgotter.github.io/berezhok/")
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "30"))
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "ironotter").lstrip("@").lower()
PHOTO_DIR = os.environ.get(
    "PHOTO_DIR", os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "uploads")
)
DAY = 86400000
WELCOME_TEXT = (
    "<b>Привет! Я Бережок 🌿</b>\n"
    "Список на подумать, прежде чем купить.\n\n"
    "Сюда складывают вещи, которые хочется купить, но не хочется брать "
    "сгоряча.\n\n"
    "<b>Как всё работает:</b>\n"
    "🔗 Пришли мне ссылку — я постараюсь найти название, цену и фотографию. "
    "Ты всё проверишь прямо в чате.\n"
    "➕ Или открой Бережка и добавь вещь вручную.\n"
    "⏳ Выбери срок ожидания. Когда он закончится, я напомню и спрошу: "
    "всё ещё хочется?\n"
    "✨ Потом можно купить вещь, отказаться от неё, оставить в желаниях или "
    "подождать ещё.\n\n"
    "Я ничего не запрещаю, просто даю каждой хотелке немного времени и "
    "помогаю беречь деньги для того, что правда радует.\n\n"
    "Начнём? 👇"
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logger = logging.getLogger(__name__)
monitor_alert_times = {}

init_db()
os.makedirs(PHOTO_DIR, exist_ok=True)


def open_app_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть Бережка", web_app=WebAppInfo(url=WEBAPP_URL))]
        ]
    )


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        WELCOME_TEXT,
        parse_mode="HTML",
        reply_markup=open_app_keyboard(),
    )


def monitor_chat_id():
    conn = get_conn()
    row = conn.execute(
        "SELECT value FROM app_config WHERE key='monitor_chat_id'"
    ).fetchone()
    conn.close()
    try:
        return int(row["value"]) if row else None
    except (TypeError, ValueError):
        return None


async def send_monitor_alert(key: str, text: str, cooldown_seconds: int = 3600):
    chat_id = monitor_chat_id()
    if not chat_id:
        return
    now = time.monotonic()
    if now - monitor_alert_times.get(key, 0) < cooldown_seconds:
        return
    try:
        await bot.send_message(chat_id, text)
        monitor_alert_times[key] = now
    except Exception:
        logger.exception("Не удалось отправить сообщение в Пультовую")


@dp.message(Command("monitor_here"))
async def register_monitor_chat(message: Message):
    username = (message.from_user.username or "").lower()
    if username != OWNER_USERNAME:
        await message.answer("Эту команду может использовать только создательница Бережка.")
        return
    conn = get_conn()
    conn.execute(
        "INSERT INTO app_config (key, value) VALUES ('monitor_chat_id', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(message.chat.id),),
    )
    conn.commit()
    conn.close()
    await message.answer(
        "🟢 Пультовая подключена. Сюда будут приходить технические сигналы Бережка."
    )


@dp.message(Command("monitor_status"))
async def monitor_status(message: Message):
    username = (message.from_user.username or "").lower()
    if username != OWNER_USERNAME:
        return
    conn = get_conn()
    heartbeat = conn.execute(
        "SELECT value FROM app_config WHERE key='bot_heartbeat_ms'"
    ).fetchone()
    backup = conn.execute(
        "SELECT value FROM app_config WHERE key='last_backup_ms'"
    ).fetchone()
    conn.close()
    now = now_ms()
    bot_age = round((now - int(heartbeat["value"])) / 1000) if heartbeat else None
    backup_age = round((now - int(backup["value"])) / 3600000, 1) if backup else None
    await message.answer(
        "🟢 Бот работает"
        + (f" · последний сигнал {bot_age} сек. назад" if bot_age is not None else "")
        + (f"\n🛟 Последний бэкап {backup_age} ч. назад" if backup_age is not None else "")
    )


def draft_keyboard(row):
    draft_id = row["draft_id"]
    needs_name = row["name"].startswith("Вещь с ")
    if needs_name:
        first_rows = [
            [InlineKeyboardButton(text="✏️ Название и цена", callback_data=f"draft:details:{draft_id}")],
            [InlineKeyboardButton(text="Добавить как есть", callback_data=f"draft:add:{draft_id}")],
        ]
    elif row["price"]:
        first_rows = [
            [InlineKeyboardButton(text="Да, добавить", callback_data=f"draft:add:{draft_id}")],
            [
                InlineKeyboardButton(text="✏️ Название", callback_data=f"draft:name:{draft_id}"),
                InlineKeyboardButton(text="💰 Цена", callback_data=f"draft:price:{draft_id}"),
            ],
        ]
    else:
        first_rows = [
            [InlineKeyboardButton(text="💰 Указать цену", callback_data=f"draft:price:{draft_id}")],
            [InlineKeyboardButton(text="Добавить без цены", callback_data=f"draft:add:{draft_id}")],
            [InlineKeyboardButton(text="✏️ Изменить название", callback_data=f"draft:name:{draft_id}")],
        ]
    return InlineKeyboardMarkup(
        inline_keyboard=first_rows + [
            [InlineKeyboardButton(text="Не добавлять", callback_data=f"draft:cancel:{draft_id}")]
        ]
    )


def draft_text(row):
    if row["name"].startswith("Вещь с "):
        return (
            "Этот сайт не отдал мне данные карточки. Ссылка сохранена, "
            "а название и цену можно быстро указать кнопками ниже."
        )
    if row["price"]:
        return f"Это «{row['name']}» за {row['price']}, верно?"
    return (
        f"Похоже, это «{row['name']}». Цену я не нашёл. "
        "Сначала укажем цену или добавим вещь без неё?"
    )


def draft_photo_path(filename):
    if not filename or os.path.basename(filename) != filename:
        return None
    return os.path.join(PHOTO_DIR, filename)


def remove_draft_photo(filename):
    path = draft_photo_path(filename)
    if not path or not filename.startswith("draft-"):
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def save_draft_photo(raw: bytes, user_id: int, draft_id: str):
    try:
        with Image.open(io.BytesIO(raw)) as source:
            image = ImageOps.exif_transpose(source).copy()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        return None
    image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (248, 245, 238))
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")
    filename = f"draft-{user_id}-{draft_id}.jpg"
    image.save(draft_photo_path(filename), format="JPEG", quality=84, optimize=True)
    return filename


async def send_draft_confirmation(message: Message, row):
    text = draft_text(row)
    keyboard = draft_keyboard(row)
    photo_path = draft_photo_path(row["photo_filename"])
    if photo_path and os.path.isfile(photo_path):
        await message.answer_photo(
            FSInputFile(photo_path), caption=text, reply_markup=keyboard
        )
    else:
        await message.answer(text, reply_markup=keyboard)


async def disable_draft_buttons(callback: CallbackQuery):
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


async def finish_draft_message(callback: CallbackQuery, text: str):
    try:
        if callback.message.photo:
            await callback.message.edit_caption(
                caption=text, reply_markup=open_app_keyboard()
            )
        else:
            await callback.message.edit_text(text, reply_markup=open_app_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=open_app_keyboard())


def message_url(message: Message):
    for text, entities in (
        (message.text or "", message.entities or []),
        (message.caption or "", message.caption_entities or []),
    ):
        for entity in entities:
            if entity.type == "text_link" and entity.url:
                candidate = entity.url
            elif entity.type == "url":
                candidate = entity.extract_from(text)
            else:
                continue
            if candidate.startswith(("http://", "https://")):
                return candidate.rstrip(".,!?;:)]}")
        match = re.search(r"https?://[^\s<>()]+", text)
        if match:
            return match.group(0).rstrip(".,!?;:)]}")
        match = re.search(r"(?<![\w@])www\.[^\s<>()]+", text, flags=re.IGNORECASE)
        if match:
            return "https://" + match.group(0).rstrip(".,!?;:)]}")
    return ""


def shared_name_hint(text: str, url: str) -> str:
    hint = text.replace(url, " ")
    hint = re.sub(r"https?://[^\s<>()]+", " ", hint)
    hint = re.sub(r"(?<![\w@])www\.[^\s<>()]+", " ", hint, flags=re.IGNORECASE)
    hint = " ".join(hint.strip(" \n\t—–-:,.!?").split())
    if not 2 <= len(hint) <= 200:
        return ""
    return hint


def parse_draft_details(text: str):
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    if len(lines) >= 2:
        name = lines[0][:200]
        price = normalize_user_price(" ".join(lines[1:]))
        return (name, price) if name and price else None
    for separator in (" — ", " – ", " - ", ", "):
        if separator not in text:
            continue
        name, price = text.rsplit(separator, 1)
        name = " ".join(name.split())[:200]
        price = normalize_user_price(price)
        if name and price:
            return name, price
    return None


@dp.message(F.text | F.caption)
async def catch_shared_link(message: Message):
    message_text = message.text or message.caption or ""
    if not message_text:
        return
    conn = get_conn()
    draft = conn.execute(
        "SELECT * FROM link_drafts WHERE user_id=?", (message.from_user.id,)
    ).fetchone()
    if draft and draft["edit_field"] in {"name", "price", "details"}:
        if message_text.strip().lower() in {"отмена", "/cancel"}:
            conn.execute(
                "UPDATE link_drafts SET edit_field=NULL WHERE user_id=?",
                (message.from_user.id,),
            )
            conn.commit()
            draft = conn.execute(
                "SELECT * FROM link_drafts WHERE user_id=?", (message.from_user.id,)
            ).fetchone()
            conn.close()
            await send_draft_confirmation(message, draft)
            return
        field = draft["edit_field"]
        value = " ".join(message_text.split()).strip()
        if field == "details":
            details = parse_draft_details(message_text)
            if not details:
                conn.close()
                await message.answer(
                    "Не смог разобрать. Пришли название и цену одним сообщением, "
                    "каждое с новой строки. Например:\n\nБеговая дорожка\n10 000"
                )
                return
            name, price = details
            conn.execute(
                "UPDATE link_drafts SET name=?, price=?, edit_field=NULL WHERE user_id=?",
                (name, price, message.from_user.id),
            )
            conn.commit()
            draft = conn.execute(
                "SELECT * FROM link_drafts WHERE user_id=?", (message.from_user.id,)
            ).fetchone()
            conn.close()
            await send_draft_confirmation(message, draft)
            return
        if field == "name":
            value = value[:200]
        else:
            value = normalize_user_price(value)
        if not value:
            conn.close()
            await message.answer("Нужно прислать непустое значение.")
            return
        conn.execute(
            f"UPDATE link_drafts SET {field}=?, edit_field=NULL WHERE user_id=?",
            (value, message.from_user.id),
        )
        conn.commit()
        draft = conn.execute(
            "SELECT * FROM link_drafts WHERE user_id=?", (message.from_user.id,)
        ).fetchone()
        conn.close()
        await send_draft_confirmation(message, draft)
        return
    conn.close()
    if message_text.startswith("/"):
        return
    url = message_url(message)
    if not url:
        return
    if len(url) > 2000:
        await message.answer("Ссылка слишком длинная — попробуй отправить другую.")
        return

    progress = await message.answer("Ссылку поймал 🌿 Смотрю название, цену и фотографию…")
    metadata = None
    try:
        metadata = await fetch_product_metadata(url)
    except (ProductFetchError, asyncio.TimeoutError, OSError) as exc:
        logger.info(
            "Не удалось получить карточку товара с %s: %s",
            urlparse(url).hostname or "неизвестного сайта",
            exc,
        )
    draft_id = uuid.uuid4().hex[:8]
    name = (metadata.name if metadata else "")[:200]
    if not name:
        name = shared_name_hint(message_text, url)
    if not name:
        name = f"Вещь с {urlparse(url).hostname or 'сайта'}"[:200]
    price = (metadata.price if metadata else "")[:100]
    photo_filename = None
    if metadata and metadata.image_url:
        try:
            raw_photo = await fetch_product_image(
                metadata.image_url, referer=metadata.page_url or url
            )
            photo_filename = save_draft_photo(raw_photo, message.from_user.id, draft_id)
        except (ProductFetchError, asyncio.TimeoutError, OSError) as exc:
            logger.info(
                "Не удалось получить изображение товара с %s: %s",
                urlparse(url).hostname or "неизвестного сайта",
                exc,
            )

    conn = get_conn()
    old = conn.execute(
        "SELECT photo_filename FROM link_drafts WHERE user_id=?", (message.from_user.id,)
    ).fetchone()
    conn.execute(
        "INSERT INTO link_drafts "
        "(user_id, draft_id, url, name, price, photo_filename, edit_field, created_at) "
        "VALUES (?,?,?,?,?,?,NULL,?) "
        "ON CONFLICT(user_id) DO UPDATE SET draft_id=excluded.draft_id, "
        "url=excluded.url, name=excluded.name, price=excluded.price, "
        "photo_filename=excluded.photo_filename, edit_field=NULL, "
        "created_at=excluded.created_at",
        (
            message.from_user.id, draft_id, url, name, price,
            photo_filename, now_ms(),
        ),
    )
    conn.execute("DELETE FROM pending_links WHERE user_id=?", (message.from_user.id,))
    conn.commit()
    draft = conn.execute(
        "SELECT * FROM link_drafts WHERE user_id=?", (message.from_user.id,)
    ).fetchone()
    conn.close()
    if old:
        remove_draft_photo(old["photo_filename"])
    try:
        await progress.delete()
    except Exception:
        pass
    await send_draft_confirmation(message, draft)


@dp.callback_query(F.data.startswith("draft:"))
async def on_draft_action(callback: CallbackQuery):
    _, action, draft_id = callback.data.split(":", 2)
    conn = get_conn()
    draft = conn.execute(
        "SELECT * FROM link_drafts WHERE user_id=? AND draft_id=?",
        (callback.from_user.id, draft_id),
    ).fetchone()
    if not draft:
        conn.close()
        await callback.answer("Этот черновик уже неактуален", show_alert=True)
        return
    if action in {"name", "price", "details"}:
        conn.execute(
            "UPDATE link_drafts SET edit_field=? WHERE user_id=? AND draft_id=?",
            (action, callback.from_user.id, draft_id),
        )
        conn.commit()
        conn.close()
        await disable_draft_buttons(callback)
        prompt = (
            "Пришли название и цену одним сообщением, каждое с новой строки. "
            "Например:\n\nБеговая дорожка\n10 000\n\n"
            "Для выхода напиши «Отмена»."
            if action == "details"
            else "Пришли новое название одним сообщением. Для выхода напиши «Отмена»."
            if action == "name"
            else "Пришли новую цену, например: 10000, 5 косарей, 300 баксов или $250. Для выхода напиши «Отмена»."
        )
        await callback.message.answer(prompt)
        await callback.answer()
        return
    if action == "cancel":
        conn.execute(
            "DELETE FROM link_drafts WHERE user_id=? AND draft_id=?",
            (callback.from_user.id, draft_id),
        )
        conn.commit()
        conn.close()
        remove_draft_photo(draft["photo_filename"])
        await disable_draft_buttons(callback)
        await callback.message.answer("Хорошо, не добавляю.")
        await callback.answer()
        return
    if action != "add":
        conn.close()
        await callback.answer("Неизвестное действие")
        return

    item_id = new_id()
    item_photo = None
    old_path = draft_photo_path(draft["photo_filename"])
    new_path = None
    if old_path and os.path.isfile(old_path):
        item_photo = f"{item_id}.jpg"
        new_path = draft_photo_path(item_photo)
        os.replace(old_path, new_path)
    try:
        conn.execute(
            "INSERT INTO items "
            "(id, user_id, name, url, price, reason, wait_days, added_at, photo_filename) "
            "VALUES (?,?,?,?,?,'',NULL,?,?)",
            (
                item_id, callback.from_user.id, draft["name"], draft["url"],
                draft["price"], now_ms(), item_photo,
            ),
        )
        conn.execute(
            "DELETE FROM link_drafts WHERE user_id=? AND draft_id=?",
            (callback.from_user.id, draft_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        if new_path and os.path.isfile(new_path):
            os.replace(new_path, old_path)
        conn.close()
        await callback.answer("Не получилось добавить вещь", show_alert=True)
        return
    conn.close()
    await finish_draft_message(
        callback, f"Добавил «{draft['name']}» в Бережок. Отсчёт уже начался 🌿"
    )
    await callback.answer("Добавлено")


def user_word(pronoun, feminine, masculine):
    return masculine if pronoun == "he" else feminine


@dp.callback_query(
    F.data.startswith("keep:")
    | F.data.startswith("drop:")
    | F.data.startswith("bought:")
)
async def on_decision(callback: CallbackQuery):
    action, item_id = callback.data.split(":", 1)
    conn = get_conn()
    row = conn.execute(
        """
        SELECT items.*, settings.self_pronoun
        FROM items
        LEFT JOIN settings ON settings.user_id = items.user_id
        WHERE items.id=? AND items.user_id=? AND items.deleted_at IS NULL
        """,
        (item_id, callback.from_user.id),
    ).fetchone()
    if not row:
        conn.close()
        await callback.answer("Эта вещь уже решена или удалена")
        return
    conn.execute(
        "UPDATE items SET decision=?, decided_at=?, archived=1 WHERE id=?",
        (action, now_ms(), item_id),
    )
    conn.commit()
    conn.close()
    pronoun = row["self_pronoun"] or "she"
    if action == "keep":
        text = user_word(pronoun, "Записала", "Записал") + " — вещь остаётся в желаниях 🙂"
    elif action == "bought":
        text = user_word(pronoun, "Купила", "Купил") + " — пусть радует!"
    else:
        text = user_word(pronoun, "Убрала", "Убрал") + " из списка"
    await callback.message.edit_text(text)
    await callback.answer()


@dp.callback_query(F.data.startswith("snooze:"))
async def on_snooze(callback: CallbackQuery):
    _, days_text, item_id = callback.data.split(":", 2)
    days = float(days_text)
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM items WHERE id=? AND user_id=? AND deleted_at IS NULL",
        (item_id, callback.from_user.id),
    ).fetchone()
    if not row:
        conn.close()
        await callback.answer("Эта вещь уже решена или удалена")
        return
    conn.execute(
        "UPDATE items SET added_at=?, wait_days=?, decision=NULL, decided_at=NULL, "
        "archived=0, notified=0 WHERE id=? AND user_id=?",
        (now_ms(), days, item_id, callback.from_user.id),
    )
    conn.commit()
    conn.close()
    await callback.message.edit_text(f"Хорошо, вернусь к этому через {int(days)} дн.")
    await callback.answer()


async def reminder_loop():
    # Раз в CHECK_INTERVAL_SECONDS проверяет, у кого истёк срок ожидания,
    # и шлёт сообщение с кнопками прямо в чат — то, чего не было в версии без сервера.
    while True:
        conn = None
        try:
            conn = get_conn()
            now = now_ms()
            conn.execute(
                "INSERT INTO app_config (key, value) VALUES ('bot_heartbeat_ms', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(now),),
            )
            conn.commit()
            rows = conn.execute(
                """
                SELECT items.*,
                       settings.default_wait_days AS default_wait,
                       settings.self_pronoun AS self_pronoun
                FROM items
                LEFT JOIN settings ON settings.user_id = items.user_id
                WHERE items.decision IS NULL AND items.archived = 0
                  AND items.notified = 0 AND items.deleted_at IS NULL
                """
            ).fetchall()
            for r in rows:
                wait = r["wait_days"] if r["wait_days"] is not None else (r["default_wait"] or 7)
                ready_at = r["added_at"] + wait * DAY
                if now >= ready_at:
                    bought_text = user_word(
                        r["self_pronoun"] or "she", "Купила", "Купил"
                    )
                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(text=bought_text, callback_data=f"bought:{r['id']}"),
                                InlineKeyboardButton(text="Уже не надо", callback_data=f"drop:{r['id']}"),
                            ],
                            [
                                InlineKeyboardButton(text="Ещё 3 дня", callback_data=f"snooze:3:{r['id']}"),
                                InlineKeyboardButton(text="Ещё неделю", callback_data=f"snooze:7:{r['id']}"),
                            ],
                            [InlineKeyboardButton(text="Оставить в желаниях", callback_data=f"keep:{r['id']}")],
                        ]
                    )
                    try:
                        reason = ""
                        if r["reason"]:
                            reason_intro = user_word(
                                r["self_pronoun"] or "she",
                                "Ты хотела этого потому, что",
                                "Ты хотел этого потому, что",
                            )
                            reason = f"\n{reason_intro}: {r['reason']}"
                        await bot.send_message(
                            r["user_id"],
                            f"Прошло время — всё ещё хочешь «{r['name']}»?{reason}",
                            reply_markup=keyboard,
                        )
                        conn.execute("UPDATE items SET notified=1 WHERE id=?", (r["id"],))
                        conn.commit()
                    except Exception:
                        logger.exception(
                            "Не удалось отправить напоминание для item_id=%s", r["id"]
                        )
                        await send_monitor_alert(
                            f"reminder:{r['id']}",
                            "🟠 Бережок не смог отправить одно из напоминаний. "
                            "Он попробует снова автоматически.",
                        )
            # Тихое напоминание приходит только после недели без открытия
            # приложения и только днём. Активным людям оно не мешает.
            if 8 <= datetime.now(timezone.utc).hour < 19:
                for setting in reminder_candidates(conn, now):
                    try:
                        await bot.send_message(
                            setting["user_id"],
                            reminder_text(setting["user_id"], now),
                            reply_markup=open_app_keyboard(),
                        )
                    except Exception:
                        logger.exception(
                            "Не удалось отправить тихое напоминание user_id=%s",
                            setting["user_id"],
                        )
                    finally:
                        # Не повторяем попытку каждые 30 секунд: даже при временной
                        # ошибке следующий аккуратный сигнал будет не раньше недели.
                        conn.execute(
                            "UPDATE settings SET last_gentle_reminder_at=? "
                            "WHERE user_id=?",
                            (now, setting["user_id"]),
                        )
                        conn.commit()
        except Exception:
            logger.exception("Ошибка цикла напоминаний")
            await send_monitor_alert(
                "reminder-loop",
                "🔴 Ошибка цикла напоминаний. Проверь журнал berezhok-bot.",
            )
        finally:
            if conn is not None:
                conn.close()
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def main():
    reminder_task = asyncio.create_task(reminder_loop())
    await send_monitor_alert(
        "bot-started", "🟢 Бот Бережка запущен и проверяет напоминания.", 0
    )
    try:
        await dp.start_polling(bot)
    finally:
        reminder_task.cancel()
        with suppress(asyncio.CancelledError):
            await reminder_task


if __name__ == "__main__":
    asyncio.run(main())
