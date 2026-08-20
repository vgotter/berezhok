import hashlib
import hmac
import io
import json
import math
import os
import shutil
import threading
import time
import urllib.parse
import warnings
import zipfile
from collections import defaultdict, deque
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from analytics import track_event
from db import DB_PATH, get_conn, init_db, new_id, now_ms

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "https://vgotter.github.io")
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "ironotter").lstrip("@").lower()
AUTH_MAX_AGE_SECONDS = int(os.environ.get("AUTH_MAX_AGE_SECONDS", "86400"))
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "240"))
BOT_HEARTBEAT_MAX_AGE_SECONDS = int(
    os.environ.get("BOT_HEARTBEAT_MAX_AGE_SECONDS", "120")
)
BACKUP_MAX_AGE_SECONDS = int(os.environ.get("BACKUP_MAX_AGE_SECONDS", "172800"))
MIN_FREE_DISK_BYTES = int(os.environ.get("MIN_FREE_DISK_BYTES", str(200 * 1024 * 1024)))
S3_BUCKET = os.environ.get("S3_BUCKET", "").strip()
DAY = 86400000
PHOTO_DIR = os.environ.get(
    "PHOTO_DIR", os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "uploads")
)
MAX_PHOTO_BYTES = 12 * 1024 * 1024
MAX_PHOTO_SIDE = 1600
request_times = defaultdict(deque)
request_times_lock = threading.Lock()

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()
os.makedirs(PHOTO_DIR, exist_ok=True)


def check_init_data(init_data: str) -> dict:
    # Проверка подписи Telegram WebApp initData — подтверждает, что запрос
    # действительно пришёл из Mini App конкретного пользователя, без пароля.
    # https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        raise HTTPException(401, "bad init data")
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "no hash")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        raise HTTPException(401, "invalid signature")
    try:
        auth_date = int(parsed.get("auth_date", ""))
    except ValueError:
        raise HTTPException(401, "bad auth date")
    age = int(time.time()) - auth_date
    if age < -30 or age > AUTH_MAX_AGE_SECONDS:
        raise HTTPException(401, "expired init data")
    if "user" not in parsed:
        raise HTTPException(401, "no user")
    try:
        user = json.loads(parsed["user"])
        user_id = int(user["id"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise HTTPException(401, "bad user")
    if user_id <= 0:
        raise HTTPException(401, "bad user")
    user["id"] = user_id
    return user


def enforce_rate_limit(user_id: int) -> None:
    if RATE_LIMIT_PER_MINUTE <= 0:
        return
    now = time.monotonic()
    with request_times_lock:
        timestamps = request_times[user_id]
        while timestamps and now - timestamps[0] >= 60:
            timestamps.popleft()
        if len(timestamps) >= RATE_LIMIT_PER_MINUTE:
            raise HTTPException(429, "too many requests")
        timestamps.append(now)


def get_user(x_telegram_init_data: str = Header(...)) -> dict:
    user = check_init_data(x_telegram_init_data)
    enforce_rate_limit(user["id"])
    return user


def get_user_id(user: dict = Depends(get_user)) -> int:
    return user["id"]


class ItemIn(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    name: str = Field(min_length=1, max_length=200)
    url: str = Field(default="", max_length=2000)
    price: str = Field(default="", max_length=100)
    reason: str = Field(default="", max_length=500)
    waitDays: Optional[float] = Field(default=None, ge=0, le=3650)


class DecisionIn(BaseModel):
    decision: str


class SnoozeIn(BaseModel):
    days: float


class NeedTestIn(BaseModel):
    answers: list[int] = Field(min_length=7, max_length=7)


class SettingsIn(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    defaultWaitDays: Optional[float] = None
    hideWaiting: Optional[bool] = None
    archiveAction: Optional[str] = None
    archiveAfterDays: Optional[float] = None
    selfPronoun: Optional[str] = None
    gentleReminders: Optional[bool] = None


class DeleteAccountIn(BaseModel):
    confirmation: str


class AnalyticsEventIn(BaseModel):
    event: str


def row_to_item(r):
    return {
        "id": r["id"],
        "name": r["name"],
        "url": r["url"],
        "price": r["price"],
        "reason": r["reason"] or "",
        "waitDays": r["wait_days"],
        "addedAt": r["added_at"],
        "decision": r["decision"],
        "decidedAt": r["decided_at"],
        "archived": bool(r["archived"]),
        "hasPhoto": bool(r["photo_filename"]),
        "needTestResult": r["need_test_result"],
        "needTestCompletedAt": r["need_test_completed_at"],
    }


def photo_path(filename: str) -> str:
    # Имена создаются только сервером, но эта проверка не позволит когда-либо
    # прочитать файл за пределами PHOTO_DIR, даже если база будет повреждена.
    if not filename or os.path.basename(filename) != filename:
        raise HTTPException(404, "photo not found")
    return os.path.join(PHOTO_DIR, filename)


def remove_photo(filename: Optional[str]) -> None:
    if not filename:
        return
    try:
        os.remove(photo_path(filename))
    except (FileNotFoundError, HTTPException):
        pass


async def store_photo(upload: UploadFile, item_id: str) -> str:
    if upload.content_type and not upload.content_type.startswith("image/"):
        raise HTTPException(415, "file must be an image")

    raw = await upload.read(MAX_PHOTO_BYTES + 1)
    if not raw:
        raise HTTPException(400, "empty photo")
    if len(raw) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "photo is too large")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                source.seek(0)
                image = ImageOps.exif_transpose(source).copy()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise HTTPException(415, "unsupported or damaged image")

    image.thumbnail((MAX_PHOTO_SIDE, MAX_PHOTO_SIDE), Image.Resampling.LANCZOS)
    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (248, 245, 238))
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")

    filename = f"{item_id}.jpg"
    target = photo_path(filename)
    temporary = target + ".tmp"
    try:
        image.save(temporary, format="JPEG", quality=84, optimize=True)
        os.replace(temporary, target)
    finally:
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass
    return filename


def settings_to_dict(row):
    return {
        "defaultWaitDays": row["default_wait_days"],
        "hideWaiting": bool(row["hide_waiting"]),
        "archiveAction": row["archive_action"],
        "archiveAfterDays": row["archive_after_days"],
        "selfPronoun": row["self_pronoun"] or "she",
        "gentleReminders": bool(row["gentle_reminders"]),
    }


def load_settings(conn, user_id):
    row = conn.execute("SELECT * FROM settings WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        conn.execute("INSERT INTO settings (user_id) VALUES (?)", (user_id,))
        conn.commit()
        row = conn.execute("SELECT * FROM settings WHERE user_id=?", (user_id,)).fetchone()
    return row


def sweep(conn, user_id, settings_row):
    # Переносит в архив вещи, по которым не приняли решение за archive_after_days
    # после того, как истёк срок ожидания — то же самое, что раньше делал браузер.
    now = now_ms()
    rows = conn.execute(
        "SELECT * FROM items WHERE user_id=? AND decision IS NULL AND archived=0 "
        "AND deleted_at IS NULL",
        (user_id,),
    ).fetchall()
    for r in rows:
        wait = r["wait_days"] if r["wait_days"] is not None else settings_row["default_wait_days"]
        ready_at = r["added_at"] + wait * DAY
        if now >= ready_at + settings_row["archive_after_days"] * DAY:
            if settings_row["archive_action"] == "delete":
                conn.execute(
                    "UPDATE items SET deleted_at=? WHERE id=?", (now, r["id"])
                )
            else:
                conn.execute(
                    "UPDATE items SET archived=1, decision='expired', decided_at=? WHERE id=?",
                    (now, r["id"]),
                )
    conn.commit()


@app.get("/api/state")
def get_state(user: dict = Depends(get_user)):
    user_id = user["id"]
    conn = get_conn()
    settings_row = load_settings(conn, user_id)
    sweep(conn, user_id, settings_row)
    seen_at = now_ms()
    conn.execute(
        "UPDATE settings SET last_seen_at=? WHERE user_id=?", (seen_at, user_id)
    )
    track_event(conn, user_id, "app_open", seen_at, "app", unique_daily=True)
    conn.commit()
    items = conn.execute(
        "SELECT * FROM items WHERE user_id=? AND deleted_at IS NULL ORDER BY added_at",
        (user_id,),
    ).fetchall()
    conn.close()
    return {
        "items": [row_to_item(r) for r in items],
        "settings": settings_to_dict(settings_row),
        "features": {
            "testWaits": (user.get("username") or "").lower() == OWNER_USERNAME,
        },
    }


@app.post("/api/items")
def create_item(item: ItemIn, user_id: int = Depends(get_user_id)):
    name = item.name.strip()
    if not name:
        raise HTTPException(422, "name must contain 1 to 200 characters")
    conn = get_conn()
    item_id = new_id()
    created_at = now_ms()
    conn.execute(
        "INSERT INTO items (id, user_id, name, url, price, reason, wait_days, added_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            item_id, user_id, name, item.url.strip(), item.price.strip(), item.reason.strip(),
            item.waitDays, created_at,
        ),
    )
    track_event(conn, user_id, "item_added", created_at, "app")
    conn.commit()
    conn.close()
    return {"id": item_id}


@app.post("/api/items-with-photo")
async def create_item_with_photo(
    name: str = Form(...),
    url: str = Form(""),
    price: str = Form(""),
    reason: str = Form(""),
    waitDays: Optional[float] = Form(None),
    photo: Optional[UploadFile] = File(None),
    user_id: int = Depends(get_user_id),
):
    name = name.strip()
    url = url.strip()
    price = price.strip()
    reason = reason.strip()
    if not name or len(name) > 200:
        raise HTTPException(422, "name must contain 1 to 200 characters")
    if len(url) > 2000 or len(price) > 100 or len(reason) > 500:
        raise HTTPException(422, "item fields are too long")
    if waitDays is not None and (
        not math.isfinite(waitDays) or waitDays < 0 or waitDays > 3650
    ):
        raise HTTPException(422, "invalid wait time")

    item_id = new_id()
    filename = None
    if photo is not None and photo.filename:
        filename = await store_photo(photo, item_id)

    conn = get_conn()
    created_at = now_ms()
    try:
        conn.execute(
            "INSERT INTO items "
            "(id, user_id, name, url, price, reason, wait_days, added_at, photo_filename) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                item_id, user_id, name, url, price, reason, waitDays,
                created_at, filename,
            ),
        )
        track_event(conn, user_id, "item_added", created_at, "app")
        conn.commit()
    except Exception:
        remove_photo(filename)
        raise
    finally:
        conn.close()
    return {"id": item_id, "hasPhoto": bool(filename)}


def parse_wait_days(value: Optional[str]) -> Optional[float]:
    if value is None or value in ("", "default"):
        return None
    try:
        days = float(value)
    except ValueError:
        raise HTTPException(422, "invalid wait time")
    if not math.isfinite(days) or days < 0 or days > 3650:
        raise HTTPException(422, "invalid wait time")
    return days


@app.put("/api/items/{item_id}")
async def update_item(
    item_id: str,
    name: str = Form(...),
    url: str = Form(""),
    price: str = Form(""),
    reason: str = Form(""),
    waitDays: Optional[str] = Form(None),
    removePhoto: bool = Form(False),
    photo: Optional[UploadFile] = File(None),
    user_id: int = Depends(get_user_id),
):
    name = name.strip()
    url = url.strip()
    price = price.strip()
    reason = reason.strip()
    if not name or len(name) > 200:
        raise HTTPException(422, "name must contain 1 to 200 characters")
    if len(url) > 2000 or len(price) > 100 or len(reason) > 500:
        raise HTTPException(422, "item fields are too long")
    wait_days = parse_wait_days(waitDays)

    conn = get_conn()
    row = conn.execute(
        "SELECT photo_filename FROM items WHERE id=? AND user_id=? "
        "AND deleted_at IS NULL",
        (item_id, user_id),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "not found")

    old_filename = row["photo_filename"]
    new_filename = old_filename
    try:
        if photo is not None and photo.filename:
            new_filename = await store_photo(photo, item_id)
        elif removePhoto:
            new_filename = None
        conn.execute(
            "UPDATE items SET name=?, url=?, price=?, reason=?, wait_days=?, "
            "photo_filename=? WHERE id=? AND user_id=?",
            (name, url, price, reason, wait_days, new_filename, item_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()
    if removePhoto and old_filename and new_filename is None:
        remove_photo(old_filename)
    return {"ok": True, "hasPhoto": bool(new_filename)}


@app.delete("/api/items/{item_id}")
def delete_item(item_id: str, user_id: int = Depends(get_user_id)):
    conn = get_conn()
    row = conn.execute(
        "SELECT photo_filename FROM items WHERE id=? AND user_id=? "
        "AND deleted_at IS NULL",
        (item_id, user_id),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "not found")
    deleted_at = now_ms()
    conn.execute(
        "UPDATE items SET deleted_at=? WHERE id=? AND user_id=?",
        (deleted_at, item_id, user_id),
    )
    track_event(conn, user_id, "item_deleted", deleted_at, "app")
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/items/{item_id}/photo")
def get_item_photo(item_id: str, user_id: int = Depends(get_user_id)):
    conn = get_conn()
    row = conn.execute(
        "SELECT photo_filename FROM items WHERE id=? AND user_id=? "
        "AND deleted_at IS NULL",
        (item_id, user_id),
    ).fetchone()
    conn.close()
    if not row or not row["photo_filename"]:
        raise HTTPException(404, "photo not found")
    path = photo_path(row["photo_filename"])
    if not os.path.isfile(path):
        raise HTTPException(404, "photo not found")
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.post("/api/items/{item_id}/decide")
def decide_item(item_id: str, body: DecisionIn, user_id: int = Depends(get_user_id)):
    if body.decision not in {"keep", "drop", "bought"}:
        raise HTTPException(422, "invalid decision")
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM items WHERE id=? AND user_id=? AND deleted_at IS NULL",
        (item_id, user_id),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "not found")
    decided_at = now_ms()
    conn.execute(
        "UPDATE items SET decision=?, decided_at=?, archived=1 WHERE id=?",
        (body.decision, decided_at, item_id),
    )
    track_event(conn, user_id, f"decision_{body.decision}", decided_at, "app")
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/items/{item_id}/snooze")
def snooze_item(item_id: str, body: SnoozeIn, user_id: int = Depends(get_user_id)):
    if not math.isfinite(body.days) or body.days <= 0 or body.days > 3650:
        raise HTTPException(422, "invalid snooze time")
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM items WHERE id=? AND user_id=? AND deleted_at IS NULL",
        (item_id, user_id),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "not found")
    snoozed_at = now_ms()
    conn.execute(
        "UPDATE items SET added_at=?, wait_days=?, decision=NULL, decided_at=NULL, "
        "archived=0, notified=0 WHERE id=? AND user_id=?",
        (snoozed_at, body.days, item_id, user_id),
    )
    track_event(conn, user_id, "snoozed", snoozed_at, "app")
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/items/{item_id}/need-test")
def save_need_test(
    item_id: str, body: NeedTestIn, user_id: int = Depends(get_user_id)
):
    if any(answer not in {0, 1, 2} for answer in body.answers):
        raise HTTPException(422, "invalid test answer")
    score = sum(body.answers)
    if score >= 10:
        result = "needed"
    elif score <= 5:
        result = "not_needed"
    else:
        result = "unclear"
    completed_at = now_ms()
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM items WHERE id=? AND user_id=? AND deleted_at IS NULL",
        (item_id, user_id),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "not found")
    conn.execute(
        "UPDATE items SET need_test_result=?, need_test_answers=?, "
        "need_test_completed_at=? WHERE id=? AND user_id=?",
        (result, json.dumps(body.answers), completed_at, item_id, user_id),
    )
    track_event(conn, user_id, "need_test_completed", completed_at, "app")
    conn.commit()
    conn.close()
    return {"result": result, "completedAt": completed_at}


@app.post("/api/items/{item_id}/undo")
def undo_item(item_id: str, user_id: int = Depends(get_user_id)):
    conn = get_conn()
    row = conn.execute(
        "SELECT deleted_at, decision, archived FROM items WHERE id=? AND user_id=?",
        (item_id, user_id),
    ).fetchone()
    if not row or not (row["deleted_at"] or row["decision"] or row["archived"]):
        conn.close()
        raise HTTPException(404, "nothing to undo")
    if row["deleted_at"]:
        conn.execute(
            "UPDATE items SET deleted_at=NULL WHERE id=? AND user_id=?",
            (item_id, user_id),
        )
    else:
        conn.execute(
            "UPDATE items SET decision=NULL, decided_at=NULL, archived=0, "
            "notified=0 WHERE id=? AND user_id=?",
            (item_id, user_id),
        )
    track_event(conn, user_id, "undo", now_ms(), "app")
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/items/{item_id}/restore")
def restore_item(item_id: str, user_id: int = Depends(get_user_id)):
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM items WHERE id=? AND user_id=? AND deleted_at IS NULL "
        "AND (archived=1 OR decision IS NOT NULL)",
        (item_id, user_id),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "not found")
    restored_at = now_ms()
    conn.execute(
        "UPDATE items SET added_at=?, decision=NULL, decided_at=NULL, archived=0, "
        "notified=0 WHERE id=? AND user_id=?",
        (restored_at, item_id, user_id),
    )
    track_event(conn, user_id, "restored", restored_at, "app")
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/pending-link/consume")
def consume_pending_link(user_id: int = Depends(get_user_id)):
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT url FROM pending_links WHERE user_id=?", (user_id,)
    ).fetchone()
    if row:
        conn.execute("DELETE FROM pending_links WHERE user_id=?", (user_id,))
        conn.commit()
    conn.close()
    return {"url": row["url"] if row else ""}


@app.post("/api/analytics")
def record_client_analytics(
    body: AnalyticsEventIn, user_id: int = Depends(get_user_id)
):
    if body.event not in {"sos_open", "search_used"}:
        raise HTTPException(422, "invalid analytics event")
    conn = get_conn()
    track_event(
        conn, user_id, body.event, now_ms(), "app", unique_daily=True
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.put("/api/settings")
def update_settings(body: SettingsIn, user_id: int = Depends(get_user_id)):
    conn = get_conn()
    load_settings(conn, user_id)
    mapping = {
        "defaultWaitDays": "default_wait_days",
        "hideWaiting": "hide_waiting",
        "archiveAction": "archive_action",
        "archiveAfterDays": "archive_after_days",
        "selfPronoun": "self_pronoun",
        "gentleReminders": "gentle_reminders",
    }
    data = body.model_dump(exclude_none=True)
    fields, values = [], []
    for key, value in data.items():
        col = mapping[key]
        if col == "self_pronoun" and value not in {"she", "he"}:
            conn.close()
            raise HTTPException(422, "invalid pronoun")
        if col == "archive_action" and value not in {"archive", "delete"}:
            conn.close()
            raise HTTPException(422, "invalid archive action")
        if col in {"default_wait_days", "archive_after_days"} and (
            not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            or value > 3650
        ):
            conn.close()
            raise HTTPException(422, "invalid settings time")
        if col in {"hide_waiting", "gentle_reminders"}:
            value = int(value)
        fields.append(f"{col}=?")
        values.append(value)
        if col == "gentle_reminders" and value:
            fields.append("last_gentle_reminder_at=?")
            values.append(now_ms())
    if fields:
        values.append(user_id)
        conn.execute(f"UPDATE settings SET {', '.join(fields)} WHERE user_id=?", values)
        conn.commit()
    conn.close()
    return {"ok": True}


def row_dict(row):
    return {key: row[key] for key in row.keys()}


@app.get("/api/account/export")
def export_account(user_id: int = Depends(get_user_id)):
    conn = get_conn()
    items = conn.execute(
        "SELECT * FROM items WHERE user_id=? ORDER BY added_at", (user_id,)
    ).fetchall()
    settings = conn.execute(
        "SELECT * FROM settings WHERE user_id=?", (user_id,)
    ).fetchone()
    pending_links = conn.execute(
        "SELECT * FROM pending_links WHERE user_id=?", (user_id,)
    ).fetchall()
    drafts = conn.execute(
        "SELECT * FROM link_drafts WHERE user_id=?", (user_id,)
    ).fetchall()
    analytics_user = conn.execute(
        "SELECT * FROM analytics_users WHERE user_id=?", (user_id,)
    ).fetchone()
    analytics_daily = conn.execute(
        "SELECT * FROM analytics_daily WHERE user_id=? ORDER BY day, event",
        (user_id,),
    ).fetchall()
    conn.close()

    payload = {
        "formatVersion": 1,
        "exportedAt": now_ms(),
        "telegramUserId": user_id,
        "settings": row_dict(settings) if settings else None,
        "items": [row_dict(row) for row in items],
        "pendingLinks": [row_dict(row) for row in pending_links],
        "linkDrafts": [row_dict(row) for row in drafts],
        "analyticsUser": row_dict(analytics_user) if analytics_user else None,
        "analyticsDaily": [row_dict(row) for row in analytics_daily],
    }
    archive_data = io.BytesIO()
    with zipfile.ZipFile(archive_data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "berezhok-data.json",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        for row in items:
            filename = row["photo_filename"]
            if not filename:
                continue
            try:
                path = photo_path(filename)
            except HTTPException:
                continue
            if os.path.isfile(path):
                archive.write(path, arcname=f"photos/{row['id']}.jpg")
        for row in drafts:
            filename = row["photo_filename"]
            if not filename:
                continue
            try:
                path = photo_path(filename)
            except HTTPException:
                continue
            if os.path.isfile(path):
                archive.write(path, arcname=f"draft-photos/{filename}")
    archive_data.seek(0)
    headers = {"Content-Disposition": 'attachment; filename="berezhok-data.zip"'}
    return StreamingResponse(archive_data, media_type="application/zip", headers=headers)


@app.post("/api/account/delete")
def delete_account(body: DeleteAccountIn, user_id: int = Depends(get_user_id)):
    if body.confirmation != "УДАЛИТЬ":
        raise HTTPException(422, "confirmation required")
    conn = get_conn()
    item_photos = conn.execute(
        "SELECT photo_filename FROM items WHERE user_id=? AND photo_filename IS NOT NULL",
        (user_id,),
    ).fetchall()
    draft_photos = conn.execute(
        "SELECT photo_filename FROM link_drafts WHERE user_id=? "
        "AND photo_filename IS NOT NULL",
        (user_id,),
    ).fetchall()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM link_drafts WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM pending_links WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM items WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM settings WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM analytics_daily WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM analytics_users WHERE user_id=?", (user_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    for row in [*item_photos, *draft_photos]:
        remove_photo(row["photo_filename"])
    return {"ok": True}


@app.get("/api/health")
def health():
    now = now_ms()
    database_ok = True
    config = {}
    try:
        conn = get_conn()
        conn.execute("SELECT 1").fetchone()
        config = {
            row["key"]: row["value"]
            for row in conn.execute(
                "SELECT key, value FROM app_config "
                "WHERE key IN "
                "('bot_heartbeat_ms', 'last_backup_ms', 'last_offsite_backup_ms')"
            ).fetchall()
        }
        conn.close()
    except Exception:
        database_ok = False

    def age_seconds(key):
        try:
            return max(0, round((now - int(config[key])) / 1000))
        except (KeyError, TypeError, ValueError):
            return None

    bot_age = age_seconds("bot_heartbeat_ms")
    backup_age = age_seconds("last_backup_ms")
    offsite_backup_age = age_seconds("last_offsite_backup_ms")
    bot_ok = bot_age is not None and bot_age <= BOT_HEARTBEAT_MAX_AGE_SECONDS
    backup_ok = backup_age is not None and backup_age <= BACKUP_MAX_AGE_SECONDS
    offsite_backup_ok = (
        not S3_BUCKET
        or (
            offsite_backup_age is not None
            and offsite_backup_age <= BACKUP_MAX_AGE_SECONDS
        )
    )
    try:
        free_disk = shutil.disk_usage(os.path.dirname(os.path.abspath(DB_PATH))).free
    except OSError:
        free_disk = 0
    disk_ok = free_disk >= MIN_FREE_DISK_BYTES
    healthy = database_ok and bot_ok and backup_ok and offsite_backup_ok and disk_ok
    payload = {
        "ok": healthy,
        "database": database_ok,
        "bot": bot_ok,
        "botHeartbeatAgeSeconds": bot_age,
        "backup": backup_ok,
        "backupAgeSeconds": backup_age,
        "offsiteBackupConfigured": bool(S3_BUCKET),
        "offsiteBackup": offsite_backup_ok,
        "offsiteBackupAgeSeconds": offsite_backup_age,
        "disk": disk_ok,
        "diskFreeBytes": free_disk,
    }
    return JSONResponse(payload, status_code=200 if healthy else 503)
