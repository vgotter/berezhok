import asyncio
import os

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from aiogram.filters import CommandStart

from db import get_conn, init_db, now_ms

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL_BASE = os.environ.get("WEBAPP_URL", "https://vgotter.github.io/berezhok/")
APP_VERSION = os.environ.get("APP_VERSION", "20260810-3")
WEBAPP_URL = (
    f"{WEBAPP_URL_BASE}{'&' if '?' in WEBAPP_URL_BASE else '?'}v={APP_VERSION}"
)
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "300"))
DAY = 86400000

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

init_db()


@dp.message(CommandStart())
async def start(message: Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть Бережка", web_app=WebAppInfo(url=WEBAPP_URL))]
        ]
    )
    await message.answer(
        "Привет! Это Бережок — список на подумать перед покупкой.\n\n"
        "Добавляй сюда то, что хочешь купить, а через выбранный срок вернёшься "
        "и решишь — всё ещё хочется или нет.",
        reply_markup=keyboard,
    )


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
        WHERE items.id=? AND items.user_id=?
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
        "SELECT id FROM items WHERE id=? AND user_id=?",
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
        try:
            conn = get_conn()
            now = now_ms()
            rows = conn.execute(
                """
                SELECT items.*,
                       settings.default_wait_days AS default_wait,
                       settings.self_pronoun AS self_pronoun
                FROM items
                LEFT JOIN settings ON settings.user_id = items.user_id
                WHERE items.decision IS NULL AND items.archived = 0 AND items.notified = 0
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
                        await bot.send_message(
                            r["user_id"],
                            f"Прошло время — всё ещё хочешь «{r['name']}»?",
                            reply_markup=keyboard,
                        )
                        conn.execute("UPDATE items SET notified=1 WHERE id=?", (r["id"],))
                        conn.commit()
                    except Exception:
                        pass
            conn.close()
        except Exception:
            pass
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def main():
    await asyncio.gather(dp.start_polling(bot), reminder_loop())


if __name__ == "__main__":
    asyncio.run(main())
