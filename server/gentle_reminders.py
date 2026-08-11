DAY = 86400000
GENTLE_REMINDER_INTERVAL_DAYS = 7

GENTLE_REMINDER_TEXTS = (
    "🌿 Я тут тихонько напомню о себе. Если за последнее время появилась "
    "хотелка — можно положить её в Бережок и дать ей немного времени.",
    "🌱 Небольшой привет от Бережка. Может, появилось что-то «очень надо»? "
    "Давай сначала положим это на подумать.",
    "👋 Бережок на связи. Если накопились хотелки, можно спокойно разобрать "
    "их — без запретов и чувства вины.",
)


def reminder_candidates(conn, now_ms):
    cutoff = now_ms - GENTLE_REMINDER_INTERVAL_DAYS * DAY
    return conn.execute(
        "SELECT user_id FROM settings "
        "WHERE gentle_reminders=1 AND last_seen_at IS NOT NULL "
        "AND last_seen_at<=? "
        "AND (last_gentle_reminder_at IS NULL OR last_gentle_reminder_at<=?)",
        (cutoff, cutoff),
    ).fetchall()


def reminder_text(user_id, now_ms):
    week = now_ms // (GENTLE_REMINDER_INTERVAL_DAYS * DAY)
    return GENTLE_REMINDER_TEXTS[(week + user_id) % len(GENTLE_REMINDER_TEXTS)]
