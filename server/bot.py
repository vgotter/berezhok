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
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://vgotter.github.io/berezhok/")
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


@dp.callback_query(F.data.startswith("keep:") | F.data.startswith("drop:"))
async def on_decision(callback: CallbackQuery):
    action, item_id = callback.data.split(":", 1)
    conn = get_conn()
    row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
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
    text = "Записала — она всё ещё в игре 🙂" if action == "keep" else "Убрала из списка"
    await callback.message.edit_text(text)
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
                SELECT items.*, settings.default_wait_days AS default_wait
                FROM items
                LEFT JOIN settings ON settings.user_id = items.user_id
                WHERE items.decision IS NULL AND items.archived = 0 AND items.notified = 0
                """
            ).fetchall()
            for r in rows:
                wait = r["wait_days"] if r["wait_days"] is not None else (r["default_wait"] or 7)
                ready_at = r["added_at"] + wait * DAY
                if now >= ready_at:
                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(text="Ещё хочу", callback_data=f"keep:{r['id']}"),
                                InlineKeyboardButton(text="Уже не надо", callback_data=f"drop:{r['id']}"),
                            ]
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
