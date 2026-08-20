from datetime import datetime, timedelta, timezone


MOSCOW = timezone(timedelta(hours=3))


def day_key(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, MOSCOW).date().isoformat()


def day_start_ms(timestamp_ms: int) -> int:
    moment = datetime.fromtimestamp(timestamp_ms / 1000, MOSCOW)
    start = datetime(moment.year, moment.month, moment.day, tzinfo=MOSCOW)
    return int(start.timestamp() * 1000)


def track_event(
    conn,
    user_id: int,
    event: str,
    timestamp_ms: int,
    source: str,
    unique_daily: bool = False,
) -> None:
    conn.execute(
        "INSERT INTO analytics_users "
        "(user_id, first_seen_at, last_seen_at, first_source) VALUES (?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
        (user_id, timestamp_ms, timestamp_ms, source),
    )
    increment = 0 if unique_daily else 1
    conn.execute(
        "INSERT INTO analytics_daily (day, user_id, event, count) VALUES (?,?,?,1) "
        "ON CONFLICT(day, user_id, event) DO UPDATE SET count=count+?",
        (day_key(timestamp_ms), user_id, event, increment),
    )


def event_count(conn, day: str, event: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(count), 0) AS total FROM analytics_daily "
        "WHERE day=? AND event=?",
        (day, event),
    ).fetchone()
    return int(row["total"] or 0)


def event_users(conn, day: str, event: str) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT user_id) AS total FROM analytics_daily "
        "WHERE day=? AND event=?",
        (day, event),
    ).fetchone()
    return int(row["total"] or 0)


def stats_snapshot(conn, timestamp_ms: int) -> dict:
    today_start = day_start_ms(timestamp_ms)
    today = day_key(timestamp_ms)
    seven_days_ago = today_start - 6 * 86400000
    thirty_days_ago = today_start - 29 * 86400000

    scalar = lambda sql, params=(): int(conn.execute(sql, params).fetchone()[0] or 0)
    decisions = {
        row["decision"]: row["total"]
        for row in conn.execute(
            "SELECT decision, COUNT(*) AS total FROM items "
            "WHERE deleted_at IS NULL AND decision IS NOT NULL GROUP BY decision"
        ).fetchall()
    }
    return {
        "total_users": scalar("SELECT COUNT(*) FROM analytics_users"),
        "new_today": scalar(
            "SELECT COUNT(*) FROM analytics_users WHERE first_seen_at>=?",
            (today_start,),
        ),
        "active_today": scalar(
            "SELECT COUNT(*) FROM analytics_users WHERE last_seen_at>=?",
            (today_start,),
        ),
        "active_7d": scalar(
            "SELECT COUNT(*) FROM analytics_users WHERE last_seen_at>=?",
            (seven_days_ago,),
        ),
        "active_30d": scalar(
            "SELECT COUNT(*) FROM analytics_users WHERE last_seen_at>=?",
            (thirty_days_ago,),
        ),
        "app_users_today": event_users(conn, today, "app_open"),
        "bot_starts_today": event_users(conn, today, "bot_start"),
        "items_today": event_count(conn, today, "item_added"),
        "links_today": event_count(conn, today, "link_shared"),
        "link_items_today": event_count(conn, today, "link_item_added"),
        "sos_today": event_users(conn, today, "sos_open"),
        "search_today": event_users(conn, today, "search_used"),
        "tests_today": event_count(conn, today, "need_test_completed"),
        "bought_today": event_count(conn, today, "decision_bought"),
        "dropped_today": event_count(conn, today, "decision_drop"),
        "wishlist_today": event_count(conn, today, "decision_keep"),
        "snoozed_today": event_count(conn, today, "snoozed"),
        "current_items": scalar(
            "SELECT COUNT(*) FROM items WHERE deleted_at IS NULL"
        ),
        "card_owners": scalar(
            "SELECT COUNT(DISTINCT user_id) FROM items WHERE deleted_at IS NULL"
        ),
        "waiting": scalar(
            "SELECT COUNT(*) FROM items WHERE deleted_at IS NULL "
            "AND archived=0 AND decision IS NULL"
        ),
        "wishlist": int(decisions.get("keep", 0)),
        "bought": int(decisions.get("bought", 0)),
        "dropped": int(decisions.get("drop", 0)),
    }


def format_stats(snapshot: dict) -> str:
    return (
        "<b>🌿 Статистика Бережка</b>\n\n"
        "<b>Сегодня</b>\n"
        f"👋 Новых: {snapshot['new_today']}\n"
        f"👀 Активных: {snapshot['active_today']} "
        f"(открывали приложение: {snapshot['app_users_today']})\n"
        f"🤖 Запускали бота: {snapshot['bot_starts_today']}\n"
        f"➕ Добавили вещей: {snapshot['items_today']} "
        f"(из ссылок: {snapshot['link_items_today']})\n"
        f"🔗 Прислали ссылок: {snapshot['links_today']}\n"
        f"🆘 Открывали SOS: {snapshot['sos_today']}\n"
        f"🧪 Проходили тест: {snapshot['tests_today']}\n"
        f"🔍 Искали вещь: {snapshot['search_today']}\n\n"
        f"✅ Решения: купили {snapshot['bought_today']} · "
        f"не понадобилось {snapshot['dropped_today']} · "
        f"в желания {snapshot['wishlist_today']} · "
        f"подождать ещё {snapshot['snoozed_today']}\n\n"
        "<b>Всего</b>\n"
        f"👥 Пользователей: {snapshot['total_users']}\n"
        f"↩️ Активны за 7 дней: {snapshot['active_7d']} · "
        f"за 30 дней: {snapshot['active_30d']}\n"
        f"🪪 С карточками: {snapshot['card_owners']}\n"
        f"🪪 Карточек сейчас: {snapshot['current_items']}\n"
        f"⏳ Ждут: {snapshot['waiting']}\n"
        f"💫 В списках желаний: {snapshot['wishlist']}\n"
        f"🛍 Куплено: {snapshot['bought']}\n"
        f"🍃 Не понадобилось: {snapshot['dropped']}\n\n"
        "<i>Статистика не хранит названия вещей, цены, ссылки, фото "
        "или ответы.</i>"
    )
