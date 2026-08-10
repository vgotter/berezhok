import hashlib
import hmac
import io
import json
import math
import os
import urllib.parse
import warnings
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel

from db import DB_PATH, get_conn, init_db, new_id, now_ms

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
DAY = 86400000
PHOTO_DIR = os.environ.get(
    "PHOTO_DIR", os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "uploads")
)
MAX_PHOTO_BYTES = 12 * 1024 * 1024
MAX_PHOTO_SIDE = 1600

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
    if "user" not in parsed:
        raise HTTPException(401, "no user")
    return json.loads(parsed["user"])


def get_user_id(x_telegram_init_data: str = Header(...)) -> int:
    return check_init_data(x_telegram_init_data)["id"]


class ItemIn(BaseModel):
    name: str
    url: str = ""
    price: str = ""
    reason: str = ""
    waitDays: Optional[float] = None


class DecisionIn(BaseModel):
    decision: str


class SnoozeIn(BaseModel):
    days: float


class SettingsIn(BaseModel):
    defaultWaitDays: Optional[float] = None
    hideWaiting: Optional[bool] = None
    archiveAction: Optional[str] = None
    archiveAfterDays: Optional[float] = None
    selfPronoun: Optional[str] = None


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
def get_state(user_id: int = Depends(get_user_id)):
    conn = get_conn()
    settings_row = load_settings(conn, user_id)
    sweep(conn, user_id, settings_row)
    items = conn.execute(
        "SELECT * FROM items WHERE user_id=? AND deleted_at IS NULL ORDER BY added_at",
        (user_id,),
    ).fetchall()
    conn.close()
    return {
        "items": [row_to_item(r) for r in items],
        "settings": settings_to_dict(settings_row),
    }


@app.post("/api/items")
def create_item(item: ItemIn, user_id: int = Depends(get_user_id)):
    conn = get_conn()
    item_id = new_id()
    conn.execute(
        "INSERT INTO items (id, user_id, name, url, price, reason, wait_days, added_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            item_id, user_id, item.name, item.url, item.price, item.reason,
            item.waitDays, now_ms(),
        ),
    )
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
    try:
        conn.execute(
            "INSERT INTO items "
            "(id, user_id, name, url, price, reason, wait_days, added_at, photo_filename) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                item_id, user_id, name, url, price, reason, waitDays,
                now_ms(), filename,
            ),
        )
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
    conn.execute(
        "UPDATE items SET deleted_at=? WHERE id=? AND user_id=?",
        (now_ms(), item_id, user_id),
    )
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
    conn.execute(
        "UPDATE items SET decision=?, decided_at=?, archived=1 WHERE id=?",
        (body.decision, now_ms(), item_id),
    )
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
    conn.execute(
        "UPDATE items SET added_at=?, wait_days=?, decision=NULL, decided_at=NULL, "
        "archived=0, notified=0 WHERE id=? AND user_id=?",
        (now_ms(), body.days, item_id, user_id),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


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
    conn.execute(
        "UPDATE items SET added_at=?, decision=NULL, decided_at=NULL, archived=0, "
        "notified=0 WHERE id=? AND user_id=?",
        (now_ms(), item_id, user_id),
    )
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
    }
    data = body.model_dump(exclude_none=True)
    fields, values = [], []
    for key, value in data.items():
        col = mapping[key]
        if col == "self_pronoun" and value not in {"she", "he"}:
            conn.close()
            raise HTTPException(422, "invalid pronoun")
        if col == "hide_waiting":
            value = int(value)
        fields.append(f"{col}=?")
        values.append(value)
    if fields:
        values.append(user_id)
        conn.execute(f"UPDATE settings SET {', '.join(fields)} WHERE user_id=?", values)
        conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True}
