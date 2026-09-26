"""Обогатитель YouTube: транскрипты → секции в заметках.

python yt_enrich.py --limit 20     # пилот: 20 новых видео
python yt_enrich.py                # все остальные

Источник: бесплатная библиотека youtube-transcript-api .
Метаданные (название/автор) — YouTube oEmbed, бесплатно.
TranscriptAPI — запасной путь (нужен настоящий ключ в .env; значения вида
"PREFIX:..." могут быть ссылками на keyring — код такие пропускает).

Заметки с `youtube:` в frontmatter получают:
- после тела:  ## 📺 <название> (автор, ссылка, резюме-выжимка)
- в конце:     ## Транскрипт (сплошной текст, без таймкодов)
Кэш: transcripts/<video_id>.json — повторные прогоны бесплатны.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from tg_common import SCRIPT_DIR

OUT = Path(os.environ.get("TG_OUT_DIR", str(SCRIPT_DIR / "output")))
CACHE = Path(os.environ.get("TG_CACHE_DIR", str(SCRIPT_DIR / "transcripts")))
TRANSCRIPTAPI = "https://transcriptapi.com/api/v2/youtube/transcript"


def get_key():
    env_key = os.environ.get("TRANSCRIPTAPI_KEY", "").strip()
    if env_key and not env_key.upper().startswith("KEYRING"):
        return env_key
    env_path = SCRIPT_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and line.split("=", 1)[0].strip() == "TRANSCRIPTAPI_KEY":
                key = line.split("=", 1)[1].strip()
                if key and not key.upper().startswith("KEYRING"):
                    return key
    return None


def fetch_via_library(video_id):
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    fetched = api.fetch(video_id, languages=["ru", "en"])
    segments = [{"text": s.text} for s in fetched]
    lang = getattr(fetched, "language_code", "") or "?"
    generated = getattr(fetched, "is_generated", None)
    if generated:
        lang += " (авто)"
    return segments, lang


def fetch_oembed(video_id):
    url = f"https://www.youtube.com/oembed?url=https://youtu.be/{video_id}&format=json"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.loads(resp.read().decode())
        return {"title": d.get("title"), "author": d.get("author_name")}
    except Exception:
        return {}


def fetch_via_transcriptapi(key, video_id):
    url = f"{TRANSCRIPTAPI}?video_url={video_id}&language=ru,en,asr&format=json&include_timestamp=false&send_metadata=true"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("User-Agent", "Mozilla/5.0")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
        meta = data.get("metadata") or {}
        return {"status": "ok", "language": data.get("language"),
                "transcript": data.get("transcript") or [],
                "metadata": {"title": meta.get("title"), "author": meta.get("author_name")}}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "no_captions", "transcript": [], "metadata": {}}
        if e.code == 429:
            print("    429 rate-limit, жду 60 сек…", flush=True)
            time.sleep(60)
            return fetch_via_transcriptapi(key, video_id)
        if e.code == 402:
            return {"status": "no_credits", "transcript": [], "metadata": {},
                    "error": "кредиты TranscriptAPI исчерпаны"}
        if e.code in (408, 503):
            if not hasattr(fetch_via_transcriptapi, "_tries"):
                fetch_via_transcriptapi._tries = {}
            n = fetch_via_transcriptapi._tries.get(video_id, 0) + 1
            if n <= 3:
                fetch_via_transcriptapi._tries[video_id] = n
                print(f"    {e.code} временный сбой, попытка {n}/3 через 4 сек…", flush=True)
                time.sleep(4)
                result = fetch_via_transcriptapi(key, video_id)
                fetch_via_transcriptapi._tries.pop(video_id, None)
                return result
            fetch_via_transcriptapi._tries.pop(video_id, None)
        body = e.read().decode(errors="replace")[:150]
        return {"status": "error", "error": f"HTTP {e.code}: {body}", "transcript": [], "metadata": {}}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}", "transcript": [], "metadata": {}}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


ZEN_GO = "https://opencode.ai/zen/go/v1"
LLM_MODEL = "mimo-v2.5"
LLM_SESSION = os.environ.get("LLM_SESSION", "memory-system-notes")  # обязателен для zen/go с 2026-09: x-opencode-session


def llm_key():
    env_key = os.environ.get("LLM_KEY", "").strip()
    if env_key and not env_key.upper().startswith("KEYRING"):
        return env_key
    env_path = SCRIPT_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and line.split("=", 1)[0].strip() == "LLM_KEY":
                k = line.split("=", 1)[1].strip()
                if k:
                    return k
    auth = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    if auth.exists():
        a = json.loads(auth.read_text(encoding="utf-8"))
        k = (a.get("opencode-go") or {}).get("key")
        if k:
            return k
    return None


def llm_summary(transcript_text, fallback):
    key = llm_key()
    if not key:
        return fallback
    text = re.sub(r"\s+", " ", transcript_text).strip()[:60000]
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "Ты делаешь краткие резюме YouTube-видео на русском языке. 3-6 предложений: суть, ключевые факты/шаги, выводы. Без вступлений и без markdown."},
            {"role": "user", "content": text},
        ],
        "max_tokens": 1500,
    }).encode()
    req = urllib.request.Request(ZEN_GO + "/chat/completions", data=body, method="POST")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0")
    req.add_header("x-opencode-session", LLM_SESSION)  # обязателен с 2026-09 (без него: 400 MissingSessionID)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            d = json.loads(resp.read().decode())
        msg = d["choices"][0]["message"]
        txt = (msg.get("content") or msg.get("reasoning_content") or "").strip()
        txt = re.sub(r"\s+", " ", txt)
        return txt if txt else fallback
    except urllib.error.HTTPError as e:
        print(f"    LLM HTTP {e.code}: {e.read().decode(errors='replace')[:80]}", flush=True)
        return fallback
    except Exception as e:
        print(f"    LLM error: {type(e).__name__}: {str(e)[:80]}", flush=True)
        return fallback


def key_valid(key):
    """Бесплатная проверка ключа через /youtube/info (кредиты не тратятся)."""
    req = urllib.request.Request(
        f"https://transcriptapi.com/api/v2/youtube/info?video_url=dQw4w9WgXcQ")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("User-Agent", "Mozilla/5.0")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        return True, "ok"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"


def fetch_transcript(video_id):
    key = get_key()
    if key:
        result = fetch_via_transcriptapi(key, video_id)
        result["source"] = "transcriptapi"
        result["fetched_at"] = now_iso()
        return result
    try:
        segments, lang = fetch_via_library(video_id)
        return {"status": "ok", "language": lang, "transcript": segments,
                "metadata": fetch_oembed(video_id), "source": "library",
                "fetched_at": now_iso()}
    except Exception as e:
        msg = str(e).lower()
        etype = type(e).__name__
        if "ipblocked" in etype.lower() or "too many requests" in msg or "ip " in msg:
            return {"status": "rate_limited", "transcript": [], "metadata": {},
                    "source": "library", "error": f"{etype}", "fetched_at": now_iso()}
        if "disabled" in msg or "no transcript" in msg or "not found" in msg or "unavailable" in msg:
            return {"status": "no_captions", "transcript": [], "metadata": fetch_oembed(video_id),
                    "source": "library", "fetched_at": now_iso()}
        return {"status": "error", "error": f"{etype}: {str(e)[:120]}", "transcript": [],
                "metadata": fetch_oembed(video_id), "source": "library", "fetched_at": now_iso()}


def llm_translate(text):
    """Перевод на русский, если текст не RU и не EN (иначе — без изменений). Пустая строка при сбое."""
    key = llm_key()
    if not key or not (text or "").strip():
        return text or ""
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "Если текст на русском или английском языке — верни его БЕЗ ИЗМЕНЕНИЙ. Если на любом другом языке — переведи на русский. Верни только результат без комментариев."},
            {"role": "user", "content": text[:8000]},
        ],
        "max_tokens": 3000,
    }).encode()
    req = urllib.request.Request(ZEN_GO + "/chat/completions", data=body, method="POST")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0")
    req.add_header("x-opencode-session", LLM_SESSION)  # обязателен с 2026-09
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            d = json.loads(resp.read().decode())
        msg = d["choices"][0]["message"]
        txt = (msg.get("content") or msg.get("reasoning_content") or "").strip()
        return re.sub(r"\s+", " ", txt)
    except Exception as e:
        print(f"    LLM translate error: {type(e).__name__}: {str(e)[:80]}", flush=True)
        return text


def gist(transcript, max_chars=320):
    text = " ".join(seg.get("text", "").strip() for seg in transcript)
    text = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"(?<=[.!?…])\s+", text)
    out = ""
    for s in sentences:
        if len(out) + len(s) > max_chars and out:
            break
        out += (" " if out else "") + s
    return out or text[:max_chars]


URL_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-(https|youtu)", re.I)
NC_MARKER = "транскрипт недоступен (нет субтитров"


def revert_failed_note(path):
    """Снять липкую заглушку с СБОЙНОЙ заметки (причина != 'нет субтитров').

    Возвращает True, если заметка была сбойной и восстановлена для повторной обработки.
    Честные 'нет субтитров' не трогаем — они финальны.
    """
    content = path.read_text(encoding="utf-8")
    if "## 📺" not in content:
        return False
    m = re.search(r"⚠️ транскрипт недоступен \((.+?)\)", content)
    if not m or m.group(1).startswith("нет субтитров"):
        return False
    content = re.sub(r"^has_transcript: false\n", "", content, flags=re.M)
    idx = content.find("\n## 📺")
    if idx != -1:
        content = content[:idx].rstrip() + "\n"
    path.write_text(content, encoding="utf-8")
    return True


def rename_if_url_named(path, title):
    """URL-имена → осмысленные по названию видео. Возвращает новый путь или None."""
    if not title or not URL_NAME_RE.match(path.stem):
        return None
    fm_msg = re.search(r"^msg_id: (\d+)$", path.read_text(encoding="utf-8"), re.M)
    msg_id = fm_msg.group(1) if fm_msg else "x"
    day = path.stem[:10]
    from tg_common import slugify
    base = f"{day}-{slugify(title, 60)}"
    new_name = base
    existing = {p.stem.casefold() for p in path.parent.glob("*.md") if p.stem != path.stem}
    if new_name.casefold() in existing:
        new_name = f"{base}-{msg_id}"
    n = 2
    while new_name.casefold() in existing:
        new_name = f"{base}-{msg_id}-{n}"
        n += 1
    new_path = path.with_name(f"{new_name}.md")
    path.rename(new_path)
    return new_path


def patch_note(path, video_id, cache_entry):
    content = path.read_text(encoding="utf-8")
    if "## 📺" in content:
        return None
    meta = cache_entry.get("metadata") or {}
    title_raw = meta.get("title") or ""
    title = title_raw or f"YouTube {video_id}"
    if title_raw and not re.search(r"[а-яА-ЯёЁ]", title_raw):
        title = llm_translate(title_raw) or title_raw
    author = meta.get("author") or cache_entry.get("metadata", {}).get("author_name") or ""
    url = f"https://youtu.be/{video_id}"
    lines = content.splitlines()
    fm_end = lines.index("---", 1)
    has_tr = cache_entry.get("status") == "ok" and cache_entry.get("transcript")
    lines.insert(fm_end, f"has_transcript: {'true' if has_tr else 'false'}")
    summary = ["", f"## 📺 {title}", ""]
    if author:
        summary.append(f"- **Автор:** {author}")
    summary.append(f"- **Видео:** {url}")
    if has_tr:
        summary.append(f"- **Резюме (черновик):** {gist(cache_entry['transcript'])}")
        summary.append(f"- **Язык субтитров:** {cache_entry.get('language') or '?'}")
    else:
        if cache_entry.get("status") == "no_captions":
            summary.append("- **Резюме:** ⚠️ транскрипт недоступен (нет субтитров у видео — даже автоматических)")
        else:
            reason = cache_entry.get("error") or "сбой; будет перезапрошено"
            summary.append(f"- **Резюме:** ⚠️ транскрипт недоступен ({reason})")
    content = "\n".join(lines) + "\n" + "\n".join(summary) + "\n"
    if has_tr:
        full = " ".join(seg.get("text", "").strip() for seg in cache_entry["transcript"])
        full = re.sub(r"\s+", " ", full).strip()
        content += f"\n---\n\n## Транскрипт\n\n{full}\n"
    new_path = rename_if_url_named(path, meta.get("title"))
    if new_path is None:
        path.write_text(content, encoding="utf-8")
        return None
    new_path.write_text(content, encoding="utf-8")
    return (path.stem, new_path.stem)


def run_summaries(limit):
    """LLM-резюме для заметок с транскриптом и черновиком-выжимкой (параллельно, N воркеров)."""
    key = llm_key()
    if not key:
        sys.exit("[!] Нет LLM-ключа (auth.json opencode-go или LLM_KEY в .env)")
    notes = []
    for p in OUT.rglob("*.md"):
        if p.name in ("_index.md", "report.md"):
            continue
        content = p.read_text(encoding="utf-8")
        if "Резюме (черновик)" in content and "## Транскрипт" in content:
            notes.append(p)
    if limit:
        notes = notes[:limit]
    print(f"Заметок с черновик-резюме: {len(notes)}; воркеров: 4")
    done = ok = 0
    lock = __import__("threading").Lock()

    def work(p):
        nonlocal done, ok
        content = p.read_text(encoding="utf-8")
        transcript = content.split("## Транскрипт", 1)[1].strip()
        summary = llm_summary(transcript, "")
        updated = False
        if summary:
            repl = "- **Резюме:** " + summary
            new_content = re.sub(r"- \*\*Резюме \(черновик\):\*\*.*", lambda m: repl, content, count=1)
            if new_content != content:
                p.write_text(new_content, encoding="utf-8")
                updated = True
        with lock:
            done += 1
            ok += 1 if updated else 0
            print(f"  [{done}] {p.name[:55]}… {'ok' if updated else 'LLM пусто/ошибка'}", flush=True)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(work, notes))
    print(f"\nИтог: обработано {done}, обновлено {ok}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="макс. НОВЫХ видео за прогон (пилот)")
    parser.add_argument("--retry-failed", action="store_true",
                        help="снять заглушки с сбойных заметок (кроме честных 'нет субтитров') и перезапросить")
    parser.add_argument("--summaries", action="store_true",
                        help="LLM-резюме (mimo-v2.5 через zen/go/v1) для заметок с транскриптом и черновиком")
    args = parser.parse_args()

    if args.summaries:
        run_summaries(args.limit)
        return

    CACHE.mkdir(exist_ok=True)
    notes = []
    retried = 0
    for p in OUT.rglob("*.md"):
        if p.name in ("_index.md", "report.md"):
            continue
        content = p.read_text(encoding="utf-8")
        m = re.search(r"^youtube: https://youtu\.be/([\w\-]+)$", content, re.M)
        if not m:
            continue
        if args.retry_failed and revert_failed_note(p):
            retried += 1
            notes.append((p, m.group(1)))
            continue
        if "## 📺" not in content:
            notes.append((p, m.group(1)))
    print(f"Заметок к обработке: {len(notes)}" + (f" (из них разблокировано сбойных: {retried})" if args.retry_failed else ""))
    done_fetched = 0
    patched = 0
    renames = {}
    stats = {"ok": 0, "no_captions": 0, "error": 0, "cached": 0}
    for path, vid in notes:
        if not path.exists():
            continue
        cache_file = CACHE / f"{vid}.json"
        if cache_file.exists():
            entry = json.loads(cache_file.read_text(encoding="utf-8"))
            stats["cached"] += 1
        else:
            if args.limit and done_fetched >= args.limit:
                continue
            entry = fetch_transcript(vid)
            if entry.get("status") in ("ok", "no_captions"):
                cache_file.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
            done_fetched += 1
            stats[entry.get("status", "error")] = stats.get(entry.get("status", "error"), 0) + 1
            print(f"  [{done_fetched}] {vid}: {entry.get('status')}", flush=True)
            time.sleep(3)
        result = patch_note(path, vid, entry)
        if result:
            patched += 1
            if isinstance(result, tuple):
                renames[result[0]] = result[1]
    if renames:
        fixed = 0
        for md in OUT.rglob("*.md"):
            content = md.read_text(encoding="utf-8")
            new_content = content
            for old, new in renames.items():
                if f"[[{old}]]" in new_content:
                    new_content = new_content.replace(f"[[{old}]]", f"[[{new}]]")
            if new_content != content:
                md.write_text(new_content, encoding="utf-8")
                fixed += 1
        print(f"Переименовано заметок: {len(renames)}; ссылки поправлены в {fixed} файлах")
    print(f"\nИтог: секций добавлено {patched}; новых запросов {done_fetched} "
          f"(из кэша {stats['cached']}); ok={stats['ok']}, no_captions={stats['no_captions']}, "
          f"error={stats['error']}")


if __name__ == "__main__":
    main()
