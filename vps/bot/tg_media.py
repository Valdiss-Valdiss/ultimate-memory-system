"""Поллер медиа-вложений: локальный Bot API (memory_tgapi) → staging/media/.

- getUpdates long-polling (allowed_updates=channel_post), offset в state.
- Медиа (voice/video_note/audio/video, document с a/v mime) → getFile →
  локальный файл из volume tgapi → копия в staging/media/<канал>/ + sidecar JSON.
- С 2026-09 (конвертер): также фото (kind=photo) и обычные документы
  (kind=document, pdf/docx/epub/...). Крупные документы (сверх
  MAX_DOC_DOWNLOAD) — sidecar kind=too_big без файла (обогатитель поставит
  пометку и закроет терминально).
- Файлы/папки chown → 1001:986 (обогатитель работает под этим uid).
- Копия из volume удаляется (volume не растёт).
- Заметки не создаёт — это делает tg_bot.py; обогатитель сопоставит по (channel_id, msg_id).
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tg_common import slugify

API = os.environ.get("LOCAL_TG_API_URL", "http://memory_tgapi:8081")
TOKEN = os.environ["BOT_TOKEN"]
BASE = f"{API}/bot{TOKEN}"
STAGE = Path(os.environ.get("TG_STAGE_DIR", "/staging"))
STATE_DIR = Path(os.environ.get("TG_STATE_DIR", "/app/state"))
TGAPI_DIR = Path(os.environ.get("TGAPI_FILES_DIR", "/var/lib/telegram-bot-api"))
UID, GID = 1001, 986
MAX_DOC_DOWNLOAD = int(os.environ.get("MAX_DOC_DOWNLOAD_MB", "250")) * 1024 * 1024


def api(method, requests_timeout=70, **params):
    r = requests.post(f"{BASE}/{method}", data=params, timeout=requests_timeout)
    return r.json()


def load_offset():
    p = STATE_DIR / "media_offset.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("offset", 0)
        except Exception:
            return 0
    return 0


def save_offset(offset):
    p = STATE_DIR / "media_offset.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"offset": offset}), encoding="utf-8")
    tmp.replace(p)


def chown(path):
    try:
        os.chown(path, UID, GID)
    except PermissionError:
        pass


def extract_media(post):
    m = (post.get("voice") or post.get("video_note") or post.get("audio")
         or post.get("video"))
    if m:
        kind = ("voice" if post.get("voice") else
                "video_note" if post.get("video_note") else
                "audio" if post.get("audio") else "video")
        return m, kind
    doc = post.get("document")
    if doc:
        mime = (doc.get("mime_type") or "")
        if mime.startswith(("audio/", "video/")):
            return doc, "document"
        return doc, "document"  # pdf/docx/epub/... — конвертер (файл > MAX → too_big)
    photo = post.get("photo")
    if photo:
        sizes = photo if isinstance(photo, list) else [photo]
        return (sizes[-1] or {}), "photo"  # последний элемент = максимальный размер
    return None, None


def write_too_big_sidecar(dst_dir, sidecar, chat, msg_id, media, kind):
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "channel_id": chat.get("id"),
        "msg_id": msg_id,
        "kind": "too_big",
        "title": media.get("title") or media.get("file_name") or "",
        "duration": 0,
        "mime": media.get("mime_type") or "",
        "size": media.get("file_size") or 0,
        "date": None,
    }
    sidecar.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    chown(sidecar)
    sidecar.chmod(0o664)
    print(f"[!] {chat.get('title')}: {kind} {msg_id} — файл {(meta['size'] or 0) // 1024 // 1024} МБ "
          f"> лимита {MAX_DOC_DOWNLOAD // 1024 // 1024} МБ — sidecar too_big", flush=True)


def handle_post(post):
    media, kind = extract_media(post)
    if not media:
        return
    chat = post.get("chat", {})
    chat_id = chat.get("id")
    msg_id = post.get("message_id")
    if chat_id is None or msg_id is None:
        return
    file_id = media["file_id"]
    ch_slug = slugify(chat.get("title") or str(chat_id))
    dst_dir = STAGE / "media" / ch_slug
    dst_dir.mkdir(parents=True, exist_ok=True)
    chown(dst_dir)
    dst_dir.chmod(0o775)
    sidecar = dst_dir / f"c{abs(chat_id)}_{msg_id}.json"
    if kind == "document" and (media.get("file_size") or 0) > MAX_DOC_DOWNLOAD:
        write_too_big_sidecar(dst_dir, sidecar, chat, msg_id, media, kind)
        return
    finfo = api("getFile", file_id=file_id)
    if not finfo.get("ok"):
        print(f"[!] getFile {msg_id}: {finfo.get('description')}", flush=True)
        return
    file_path = finfo["result"].get("file_path") or ""
    orig = Path(file_path).name or f"file_{msg_id}"
    dst = dst_dir / f"c{abs(chat_id)}_{msg_id}_{orig}"
    if dst.exists() and sidecar.exists():
        return
    src = TGAPI_DIR / file_path
    copied = False
    if src.exists():
        try:
            shutil.copy2(src, dst)
            copied = True
            try:
                src.unlink()
            except OSError:
                pass
        except OSError as e:
            print(f"[!] copy {src}: {e}", flush=True)
    if not copied:
        try:
            r = requests.get(f"{BASE}/file/bot{TOKEN}/{file_path}", timeout=600)
            r.raise_for_status()
            dst.write_bytes(r.content)
            copied = True
        except Exception as e:
            print(f"[!] download {file_path}: {type(e).__name__}", flush=True)
            return
    meta = {
        "channel_id": chat_id,
        "msg_id": msg_id,
        "kind": kind,
        "title": media.get("title") or media.get("file_name") or "",
        "duration": media.get("duration") or 0,
        "mime": media.get("mime_type") or ("image/jpeg" if kind == "photo" else ""),
        "size": media.get("file_size") or 0,
        "date": post.get("date"),
    }
    sidecar.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    chown(dst)
    chown(sidecar)
    dst.chmod(0o664)
    sidecar.chmod(0o664)
    print(f"[+] {chat.get('title')}: {kind} {msg_id} → {dst.name} "
          f"({(meta['size'] or 0) // 1024} КБ)", flush=True)


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    me = api("getMe", requests_timeout=30)
    if not me.get("ok"):
        print(f"[!] getMe через {API}: {me}", flush=True)
        sys.exit(1)
    print(f"[OK] Поллер медиа запущен: @{me['result'].get('username')} | api: {API}", flush=True)
    offset = load_offset()
    while True:
        try:
            r = api("getUpdates", requests_timeout=75, offset=offset, timeout=50,
                    allowed_updates=json.dumps(["channel_post"]))
        except requests.RequestException as e:
            print(f"[!] getUpdates: {type(e).__name__}", flush=True)
            time.sleep(10)
            continue
        if not r.get("ok"):
            print(f"[!] {r.get('description')}", flush=True)
            time.sleep(10)
            continue
        for upd in r.get("result", []):
            offset = max(offset, upd["update_id"] + 1)
            save_offset(offset)
            post = upd.get("channel_post") or upd.get("edited_channel_post")
            if post:
                try:
                    handle_post(post)
                except Exception as e:
                    print(f"[!] {type(e).__name__}: {str(e)[:120]}", flush=True)


if __name__ == "__main__":
    main()
