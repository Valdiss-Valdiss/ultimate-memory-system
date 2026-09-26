"""Backfill: исторические медиа-посты (voice/video/audio) → скачать (user-сессия)
→ сжать аудио → Deepgram → транскрипт+резюме. 3 параллельных воркера.

Запуск: python media_backfill.py [--limit N] [--max-min M]
Источники: media_targets.json (скан) + свежий скан vault для актуальных путей.
Резюм: media_backfill_state.json. Аудио-файлы временные — удаляются.
"""
import argparse
import asyncio
import datetime
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deepgram_lib as dg
import yt_enrich as ye
from telethon import TelegramClient

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CODE = Path(__file__).resolve().parent
VAULT = Path(os.environ.get("MEMORY_VAULT_DIR", r"C:\Users\<user>\Documents\Obsidian Vault"))
TARGETS = CODE / "media_targets.json"
STATE = CODE / "media_backfill_state.json"
TMP = CODE / "dg_media_tmp"
TMP.mkdir(exist_ok=True)
TODAY = time.strftime("%Y-%m-%d")
MAX_UPLOAD = 2 * 1024 * 1024 * 1024
WORKERS = 5
DOWNLOAD_TIMEOUT = None  # потолок снят: зависание ловит watchdog (60 мин без touch)
DL_ATTEMPTS = 2

stop_event = threading.Event()
state_lock = threading.RLock()  # RLock: воркер держит лок и зовёт save_state, который тоже лочит
spent_total = [0.0]
last_progress = [time.time()]


def touch():
    last_progress[0] = time.time()


def watchdog(stall_sec=3600):
    """Нет прогресса stall_sec → жёсткий выход (exit 1). Runner перезапустит из стейта.
    Прогресс обновляется в т.ч. progress_callback загрузки (touch на каждой порции)."""
    while True:
        time.sleep(60)
        if time.time() - last_progress[0] > stall_sec:
            print(f"[watchdog] нет прогресса {stall_sec // 60} мин — жёсткий выход "
                  f"(runner перезапустит из state)", flush=True)
            sys.stdout.flush()
            os._exit(1)


def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(s):
    touch()
    with state_lock:
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE)


def scan_note_paths():
    """(channel_id, msg_id) → актуальный путь заметки (после триажа)."""
    out = {}
    for p in VAULT.rglob("*.md"):
        if p.name == "_index.md":
            continue
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:2500]
        except OSError:
            continue
        cid = re.search(r"^channel_id: (-?\d+)$", head, re.M)
        mid = re.search(r"^msg_id: (\d+)$", head, re.M)
        if cid and mid:
            out[(int(cid.group(1)), int(mid.group(1)))] = p
    return out


def load_env():
    env = {}
    for line in (CODE / ".env").read_text(encoding="utf-8-sig").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def patch_note(path, kind, dur, summary, text):
    content = path.read_text(encoding="utf-8")
    if "## Транскрипт" not in content:
        content = content.replace("tags: [telegram]",
                                  "has_transcript: true\ntags: [telegram]", 1)
        mm, ss = int(dur) // 60, int(dur) % 60
        section = (f"\n\n---\n\n## 🎧 Медиа\n\n"
                   f"- **Тип:** {kind} ({mm}:{ss:02d})\n"
                   f"- **Резюме:** {summary or '_(нет)_'}\n"
                   f"- **Транскрипт:** Deepgram Nova-3 (аудио из Telegram)\n"
                   f"\n## Транскрипт\n\n{text}\n")
        content = content.rstrip("\n") + section
        path.write_text(content, encoding="utf-8")


def worker_process(t, file_path, comp_path, note_path, key, state):
    """Тяжёлая часть (в потоке): сжатое → Deepgram → LLM → патч. Возвращает статус."""
    to_send = comp_path or file_path
    try:
        if stop_event.is_set():
            return "stopped"
        res = dg.transcribe_file(to_send, state_path=CODE / "deepgram_state.json")
        if res["status"] == "no_deepgram_credits":
            stop_event.set()
            print("[!] Кредиты Deepgram исчерпаны — стоп", flush=True)
            return "no_credits"
        if res["status"] != "ok":
            with state_lock:
                state[key] = {"status": "error", "err": (res.get("error") or "")[:80],
                              "tries": state.get(key, {}).get("tries", 0) + 1, "at": TODAY}
                save_state(state)
            print(f"  {key}: deepgram {res.get('error', '')[:60]}", flush=True)
            return "error"
        text = (res["transcript"] or "").strip()
        if not text:
            with state_lock:
                state[key] = {"status": "nospeech", "at": TODAY}
                save_state(state)
            marker = "\n\n- **Медиа:** речи нет (музыка/заставки) — расшифровка пуста"
            content = note_path.read_text(encoding="utf-8")
            if ("## Транскрипт" not in content
                    and "речи нет (музыка/заставки)" not in content):
                note_path.write_text(content.rstrip("\n") + marker + "\n", encoding="utf-8")
            print(f"  {key}: речи нет — помечено", flush=True)
            return "nospeech"
        try:
            summary = ye.llm_summary(text, "")
        except Exception:
            summary = ""
        patch_note(note_path, t.get("kind", "медиа"),
                   t.get("duration") or res["duration_sec"], summary, text)
        with state_lock:
            state[key] = {"status": "done", "cost": res["cost_usd"],
                          "dur": res["duration_sec"], "at": TODAY}
            spent_total[0] += res["cost_usd"]
            save_state(state)
        print(f"  {key}: ok ({res['duration_sec']:.0f}с, ${res['cost_usd']:.3f}) "
              f"→ {note_path.name}", flush=True)
        return "done"
    finally:
        Path(file_path).unlink(missing_ok=True)
        if comp_path:
            Path(comp_path).unlink(missing_ok=True)


async def download_with_resume(client, msg, tmp_dir, touch, key):
    """Скачивание с возобновлением: если файл нужного размера уже лежит в tmp —
    используется сразу; иначе частичный файл (имя медиа + суффиксы " (N)")
    выравнивается до 4096 и продолжается iter_download с offset. Для документов
    без имени — стабильное имя tg-<document_id>. Потолка времени нет — зависание
    ловит watchdog (touch на каждой порции)."""
    if not (msg and msg.document):
        return await msg.download_media(file=tmp_dir,
                                        progress_callback=lambda *a: touch())
    doc_size = msg.document.size
    for p in tmp_dir.glob("*"):
        try:
            if p.is_file() and p.stat().st_size == doc_size:
                print(f"    {key}: файл уже скачан целиком — использую {p.name}",
                      flush=True)
                return p
        except OSError:
            continue
    fn = None
    for a in msg.document.attributes:
        if type(a).__name__ == "DocumentAttributeFilename":
            fn = a.file_name
            break
    if not fn:
        # Telethon генерирует такое имя для документов без filename — совпадёт
        # с уже скачанными частями
        fn = "document_" + (msg.date or datetime.datetime.now()).strftime("%Y-%m-%d_%H-%M-%S") + ".mp4"
    if "." in fn:
        base, suffix = fn.rsplit(".", 1)
        suffix = "." + suffix
    else:
        base, suffix = fn, ""
    cands = [p for p in tmp_dir.glob(base + "*" + suffix)
             if p.is_file() and p.stat().st_size < doc_size]
    if cands:
        out = max(cands, key=lambda p: p.stat().st_size)
        resume_from = (out.stat().st_size // 4096) * 4096
        if out.stat().st_size != resume_from:
            os.truncate(out, resume_from)
        print(f"    {key}: возобновление с {resume_from / 1e6:.0f} МБ "
              f"(из {doc_size / 1e6:.0f})", flush=True)
    else:
        out = tmp_dir / fn
        resume_from = 0
    with open(out, "ab" if resume_from else "wb") as fh:
        async with asyncio.timeout(180):  # порция не идёт 3 мин → retry с возобновлением
            async for chunk in client.iter_download(msg.document, offset=resume_from,
                                                    request_size=512 * 1024,
                                                    file_size=doc_size):
                fh.write(chunk)
                touch()
    return out


async def run(limit, max_min):
    targets = json.loads(TARGETS.read_text(encoding="utf-8"))
    state = load_state()
    print("Сканирую vault (актуальные пути заметок)...", flush=True)
    paths = scan_note_paths()
    todo = []
    for t in targets:
        key = f"{t['channel_id']}:{t['msg_id']}"
        if state.get(key, {}).get("status") in ("done", "skip", "nospeech"):
            continue
        dur = t.get("duration") or 0
        if max_min and dur > max_min * 60:
            continue
        if (t.get("size") or 0) > MAX_UPLOAD:
            continue
        todo.append(t)
    todo.sort(key=lambda t: (t.get("size") or 0))  # маленькие файлы первыми — быстрый видимый прогресс
    if limit:
        todo = todo[:limit]
    spent = sum(v.get("cost", 0) for v in state.values())
    print(f"Целей: {len(todo)} | уже потрачено: ${spent:.2f} | воркеров: {WORKERS}",
          flush=True)
    if not todo:
        return
    env = load_env()
    session = CODE / "session" / env.get("SESSION_NAME", "vault_import")
    client = TelegramClient(str(session), int(env["API_ID"]), env["API_HASH"])
    await client.connect()
    if not await client.is_user_authorized():
        print("[!] Сессия не авторизована")
        return
    print("подключено", flush=True)
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    loop = asyncio.get_event_loop()
    pending = set()
    made = skips = errors = 0

    def drain(tag):
        nonlocal made, skips, errors
        done, pending_set = set(), set()
        for fut in pending:
            if fut.done():
                done.add(fut)
            else:
                pending_set.add(fut)
        for fut in done:
            pending.discard(fut)
            st = fut.result() or ""
            if st == "done":
                made += 1
            elif st in ("nospeech", "error"):
                skips += 1 if st == "nospeech" else 0
                errors += 1 if st == "error" else 0

    try:
        for i, t in enumerate(todo, 1):
            if stop_event.is_set():
                break
            key = f"{t['channel_id']}:{t['msg_id']}"
            while len(pending) >= WORKERS * 2:
                done_now, _ = await asyncio.wait(pending, timeout=120,
                                                 return_when=asyncio.FIRST_COMPLETED)
                drain("")
                if not done_now and pending:
                    print(f"  [hb] в работе {len(pending)} (воркеры заняты) — жду",
                          flush=True)
                if stop_event.is_set():
                    break
            if stop_event.is_set():
                break
            path = paths.get((t["channel_id"], t["msg_id"]))
            if path is None or "## Транскрипт" in path.read_text(
                    encoding="utf-8", errors="replace")[:2500]:
                with state_lock:
                    state[key] = {"status": "done", "note": "нет/уже", "at": TODAY}
                    save_state(state)
                continue
            try:
                msg = await asyncio.wait_for(
                    client.get_messages(t["channel_id"], ids=t["msg_id"]), timeout=60)
            except asyncio.TimeoutError:
                errors += 1
                with state_lock:
                    state[key] = {"status": "error", "err": "get_messages timeout",
                                  "tries": state.get(key, {}).get("tries", 0) + 1, "at": TODAY}
                    save_state(state)
                print(f"[{i}/{len(todo)}] {key}: таймаут get_messages", flush=True)
                continue
            except Exception as e:
                errors += 1
                with state_lock:
                    state[key] = {"status": "error", "err": f"{type(e).__name__}"[:60],
                                  "tries": state.get(key, {}).get("tries", 0) + 1, "at": TODAY}
                    save_state(state)
                print(f"[{i}/{len(todo)}] {key}: {type(e).__name__}", flush=True)
                continue
            if msg is None or msg.document is None:
                skips += 1
                with state_lock:
                    state[key] = {"status": "skip", "err": "нет медиа в Telegram", "at": TODAY}
                    save_state(state)
                continue
            doc = msg.document
            attrs = {type(a).__name__: a for a in doc.attributes}
            if "DocumentAttributeAnimated" in attrs or "DocumentAttributeSticker" in attrs:
                skips += 1
                with state_lock:
                    state[key] = {"status": "gif", "at": TODAY}
                    save_state(state)
                print(f"[{i}/{len(todo)}] {key}: GIF/sticker — пропущен", flush=True)
                continue
            print(f"[{i}/{len(todo)}] {key}: скачиваю ({(t.get('size') or 0) // 1024} КБ)",
                  flush=True)
            f = None
            for attempt in range(1, DL_ATTEMPTS + 1):
                touch()
                try:
                    f = await download_with_resume(client, msg, TMP, touch, key)
                    touch()
                    break
                except Exception as e:
                    print(f"[{i}/{len(todo)}] {key}: скачивание прервано "
                          f"({type(e).__name__}: {str(e)[:60]}) — попытка "
                          f"{attempt}/{DL_ATTEMPTS}; частичный файл сохранён "
                          f"для возобновления", flush=True)
            if not f:
                errors += 1
                with state_lock:
                    state[key] = {"status": "error", "err": "download timeout",
                                  "tries": state.get(key, {}).get("tries", 0) + 1,
                                  "at": TODAY}
                    save_state(state)
                continue
            if Path(f).stat().st_size < 1024:
                Path(f).unlink(missing_ok=True)
                with state_lock:
                    state[key] = {"status": "error", "err": "пустой файл", "at": TODAY}
                    save_state(state)
                print(f"[{i}/{len(todo)}] {key}: пустой файл", flush=True)
                continue
            comp, _ = await loop.run_in_executor(pool, dg.compress_audio, f)
            fut = loop.run_in_executor(pool, worker_process, t, Path(f), comp, path, key, state)
            pending.add(fut)
        if pending:
            await asyncio.wait(pending)
            drain("")
    finally:
        pool.shutdown(wait=True)
        await client.disconnect()
    print(f"=== Итог: сделано {made}, пропущено {skips}, ошибок {errors}; "
          f"потрачено ${spent_total[0] + spent:.2f} (суммарно) ===", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-min", type=int, default=0, help="только медиа короче N минут")
    args = ap.parse_args()
    threading.Thread(target=watchdog, daemon=True).start()
    asyncio.run(run(args.limit, args.max_min))
