"""Общие утилиты для Telegram-импортёра: env, slug, сериализация сообщений."""
import json
import re
import sys
import unicodedata
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

YT_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?[^\s]*v=|shorts/|live/|embed/)|youtu\.be/)([\w\-]{6,})", re.I
)


def yt_video_ids(*texts):
    """Уникальные YouTube video-id из переданных текстов, в порядке появления."""
    seen = []
    for t in texts:
        if not t:
            continue
        for vid in YT_RE.findall(t):
            if vid not in seen:
                seen.append(vid)
    return seen


def load_env():
    env_path = SCRIPT_DIR / ".env"
    env = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    missing = [k for k in ("API_ID", "API_HASH") if not env.get(k)]
    if missing:
        sys.exit(f"[!] Нет {missing} в {env_path}. Заполните .env по образцу .env.example")
    return env


def slugify(text, max_len=60):
    text = unicodedata.normalize("NFKC", (text or "")).strip()
    text = re.sub(r"[\s/\\]+", "-", text)
    text = re.sub(r"[^\w\-\u0400-\u04FF]", "", text, flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return (text[:max_len].rstrip("-")) or "без-названия"


def load_folders():
    """folders.yaml → {название канала: папка назначения}. Простые 'ключ: значение' строки."""
    path = SCRIPT_DIR / "folders.yaml"
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            if key and value:
                out[key] = value if value.endswith("/") else value + "/"
    return out


def media_kinds(msg):
    m = msg.media
    if m is None:
        return []
    from telethon.tl import types as tl

    if isinstance(m, tl.MessageMediaPhoto):
        return ["photo"]
    if isinstance(m, tl.MessageMediaWebPage):
        return ["webpage"]
    if isinstance(m, tl.MessageMediaDocument) and m.document is not None:
        kinds = []
        mime = getattr(m.document, "mime_type", "") or ""
        for attr in m.document.attributes:
            if isinstance(attr, tl.DocumentAttributeVideo):
                kinds.append("video_note" if getattr(attr, "round_message", False) else "video")
            elif isinstance(attr, tl.DocumentAttributeAudio):
                kinds.append("voice" if getattr(attr, "voice", False) else "audio")
            elif isinstance(attr, tl.DocumentAttributeSticker):
                kinds.append("sticker")
            elif isinstance(attr, tl.DocumentAttributeAnimated):
                kinds.append("animation")
            elif isinstance(attr, tl.DocumentAttributeImageSize) and mime == "image/gif":
                kinds.append("gif")
        if not kinds:
            if mime.startswith("image/"):
                kinds.append("image")
            elif mime.startswith("audio/"):
                kinds.append("audio")
            elif mime.startswith("video/"):
                kinds.append("video")
            else:
                kinds.append("document")
        return kinds
    if isinstance(m, tl.MessageMediaPoll):
        return ["poll"]
    if isinstance(m, tl.MessageMediaGeo):
        return ["geo"]
    if isinstance(m, tl.MessageMediaContact):
        return ["contact"]
    if isinstance(m, tl.MessageMediaDice):
        return ["dice"]
    return ["other"]


def serialize_entities(msg):
    if not msg.entities:
        return []
    from telethon.tl import types as tl

    kind_map = {
        tl.MessageEntityBold: "bold",
        tl.MessageEntityItalic: "italic",
        tl.MessageEntityUnderline: "underline",
        tl.MessageEntityStrike: "strike",
        tl.MessageEntitySpoiler: "spoiler",
        tl.MessageEntityCode: "code",
        tl.MessageEntityPre: "pre",
        tl.MessageEntityBlockquote: "blockquote",
        tl.MessageEntityMention: "mention",
        tl.MessageEntityMentionName: "mention_name",
        tl.MessageEntityUrl: "url",
        tl.MessageEntityEmail: "email",
        tl.MessageEntityTextUrl: "text_link",
        tl.MessageEntityHashtag: "hashtag",
        tl.MessageEntityCashtag: "cashtag",
        tl.MessageEntityBotCommand: "bot_command",
        tl.MessageEntityPhone: "phone",
    }
    out = []
    for ent in msg.entities:
        kind = next((v for t, v in kind_map.items() if isinstance(ent, t)), "other")
        item = {"type": kind, "offset": ent.offset, "length": ent.length}
        if kind == "text_link":
            item["url"] = ent.url
        if kind == "pre":
            item["lang"] = ent.language or ""
        if kind == "mention_name":
            item["user_id"] = ent.user_id
        out.append(item)
    return out


def serialize_reactions(msg):
    if not msg.reactions or not msg.reactions.results:
        return []
    out = []
    for r in msg.reactions.results:
        emo = getattr(r.reaction, "emoticon", None)
        if emo is None:
            emo = f"custom:{getattr(r.reaction, 'document_id', '?')}"
        out.append({"emoji": emo, "count": r.count})
    return out


def serialize_forward(msg):
    f = msg.fwd_from
    if f is None:
        return None
    from telethon.tl import types as tl

    origin_id = None
    if isinstance(f.from_id, tl.PeerChannel):
        origin_id = -1000000000000 - f.from_id.channel_id
    elif isinstance(f.from_id, tl.PeerChat):
        origin_id = -f.from_id.chat_id
    elif isinstance(f.from_id, tl.PeerUser):
        origin_id = f.from_id.user_id
    return {
        "origin_id": origin_id,
        "origin_name": f.from_name,
        "origin_msg_id": f.channel_post,
        "origin_date": f.date.isoformat() if f.date else None,
        "post_author": f.post_author,
    }


def rich_message_parts(msg):
    """Новые «длинные посты» Telegram (rich_message): контент лежит в
    msg.rich_message.blocks (PageBlock*), а не в message/media. Без этого
    пост выглядит пустым и отбрасывается конвейером.

    Блоки бывают контейнерами (PageBlockBlockquoteBlocks.blocks,
    PageBlockList.items и т.п.) — обходим рекурсивно.

    Возвращает (текст, доп.виды_медиа).
    """
    rm = getattr(msg, "rich_message", None)
    blocks = getattr(rm, "blocks", None) if rm is not None else None
    if not blocks:
        return "", []

    from telethon.tl import types as tl

    out, media = [], []

    def walk(bs, in_quote=False):
        for b in bs:
            kind = type(b).__name__
            if "Photo" in kind or "Cover" in kind:
                media.append("photo")
                continue
            if "Video" in kind or "Embed" in kind or "Slideshow" in kind or "Collage" in kind:
                media.append("video")
                continue
            if "Divider" in kind or "Anchor" in kind or "AuthorDate" in kind:
                continue
            nested = getattr(b, "blocks", None) or getattr(b, "items", None)
            if nested:
                walk(nested, in_quote or "Blockquote" in kind)
                continue
            t = getattr(b, "text", None)
            if t is None or isinstance(t, tl.TextEmpty):
                continue
            txt = (getattr(t, "text", "") or "").strip()
            if not txt:
                continue
            if any(x in kind for x in ("Heading", "Title", "Subtitle", "Header", "Kicker")):
                out.append("## " + txt)
            elif "Blockquote" in kind or in_quote:
                out.append("\n".join("> " + line for line in txt.splitlines()))
            else:
                out.append(txt)

    walk(blocks)
    return "\n\n".join(p for p in out if p), media


def serialize_message(msg, channel_meta):
    webpage = None
    if msg.media is not None:
        from telethon.tl import types as tl

        if isinstance(msg.media, tl.MessageMediaWebPage):
            wp = msg.media.webpage
            if isinstance(wp, tl.WebPage):
                webpage = {
                    "url": wp.url,
                    "title": wp.title,
                    "description": wp.description,
                }
    fwd = serialize_forward(msg)
    text = msg.message or ""
    media = media_kinds(msg)
    rich_text, rich_media = rich_message_parts(msg)
    if rich_text:
        text = (text.rstrip() + "\n\n" + rich_text).strip() if text.strip() else rich_text
        for k in rich_media:
            if k not in media:
                media.append(k)
    return {
        "channel_id": channel_meta["channel_id"],
        "channel": channel_meta["title"],
        "msg_id": msg.id,
        "date": msg.date.isoformat() if msg.date else None,
        "text": text,
        "entities": serialize_entities(msg),
        "forward": fwd,
        "media": media,
        "webpage": webpage,
        "views": msg.views,
        "reactions": serialize_reactions(msg),
        "post_author": msg.post_author,
        "reply_to": msg.reply_to.reply_to_msg_id if msg.reply_to else None,
        "grouped_id": msg.grouped_id,
    }


def input_peer_to_dialog_id(peer):
    from telethon.tl import types as tl

    if isinstance(peer, tl.InputPeerChannel):
        return -1000000000000 - peer.channel_id
    if isinstance(peer, tl.InputPeerChat):
        return -peer.chat_id
    if isinstance(peer, tl.InputPeerUser):
        return peer.user_id
    return None


def jdump(obj):
    return json.dumps(obj, ensure_ascii=False)
