"""ПК-догон: добирает посты, пропущенные вотчером (историей — только ПК-сессия умеет).

Запуск: python pc_catchup.py
Делает: state с VPS → новые посты → заметки (id, folder, альбомы) → обогащение
(транскрипт + LLM-резюме + описания) → ГОТОВАЯ заметка в vault. Дублей не создаёт
(проверка vault по msg_id). Обновлённый state возвращает на VPS.
"""
import asyncio
import json
import os
import time
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from telethon import TelegramClient

from tg_common import load_folders, serialize_message, slugify
import tg_floodguard as fg
import vault_guard
from tg_convert import merge_albums, build_frontmatter, note_filename, render_markdown, dup_key
from tg_common import yt_video_ids
import yt_enrich as ye
import yt_descriptions as yd

CODE = Path(__file__).resolve().parent
VAULT = Path(r"C:\Users\<user>\Documents\Obsidian Vault")
TG_DIR = VAULT / "Inbox" / "Hermes" / "Сохраненное" / "Telegram"
VPS = "memory@CHANGE-ME-server-ip"
VPS_STATE = "/opt/memory-stack/tg-bot/state/state.json"
VPS_CHANNELS = "/opt/memory-stack/tg-bot/channels.json"
VPS_FOLDERS = "/opt/memory-stack/tg-bot/folders.yaml"
VPS_KEY = r"C:\Users\<user>\.ssh\id_ed25519"
TMP = CODE / "state_vps.json"


def vps_state_download():
    try:
        subprocess.run(["scp", "-i", VPS_KEY, "-o", "StrictHostKeyChecking=no",
                        f"{VPS}:{VPS_STATE}", str(TMP)],
                       check=True, capture_output=True, timeout=60)
        return json.loads(TMP.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[!] state с VPS не получен ({type(e).__name__}), работаю по локальной копии")
        if TMP.exists():
            return json.loads(TMP.read_text(encoding="utf-8"))
        return {}


def vps_state_upload(state):
    # merge со свежим remote: бот мог обновить позиции, пока мы работали
    try:
        remote = vps_state_download()
        for k, v in remote.items():
            state[k] = max(state.get(k, 0), v)
    except Exception:
        pass
    tmp = TMP.with_suffix(".up")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    subprocess.run(["scp", "-i", VPS_KEY, "-o", "StrictHostKeyChecking=no",
                    str(tmp), f"{VPS}:{VPS_STATE}"], check=True, capture_output=True, timeout=60)
    tmp.unlink(missing_ok=True)


def vault_has_msg(channel_dir, msg_id):
    if not channel_dir.exists():
        return False
    marker = f"msg_id: {msg_id}\n"
    for p in channel_dir.glob("*.md"):
        if marker in p.read_text(encoding="utf-8", errors="replace")[:1500]:
            return True
    return False


def _scp_down(remote, local):
    subprocess.run(["scp", "-i", VPS_KEY, "-o", "StrictHostKeyChecking=no",
                    f"{VPS}:{remote}", str(local)],
                   check=True, capture_output=True, timeout=60)


def _folders_keys(path):
    ks = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and ":" in line:
                ks.add(line.partition(":")[0].strip().casefold())
    return ks


def sync_folders():
    """Авто-ведение folders.yaml и channels.json: новые каналы, зарегистрированные
    вотчером на VPS (channels.json), получают маппинг '<title>: <slug>/' в корне vault.
    Merge по ключам: существующие строки (ручные настройки) НЕ трогаются.
    Возвращает число добавленных маппингов."""
    local_folders = CODE / "folders.yaml"
    local_channels = CODE / "channels.json"
    tmp_ch = CODE / "channels_vps.json"
    tmp_folders = CODE / "folders_vps.yaml"
    try:
        _scp_down(VPS_CHANNELS, tmp_ch)
        _scp_down(VPS_FOLDERS, tmp_folders)
    except Exception as e:
        print(f"[!] folders-синк: не получил данные с VPS ({type(e).__name__})", flush=True)
        return 0
    try:
        vps_channels = json.loads(tmp_ch.read_text(encoding="utf-8"))
    except Exception:
        print("[!] folders-синк: channels.json с VPS не распарсился", flush=True)
        return 0
    # локальный channels.json = union (VPS-записи приоритетны, локальные сохраняются)
    local_ch = {}
    if local_channels.exists():
        try:
            local_ch = json.loads(local_channels.read_text(encoding="utf-8"))
        except Exception:
            local_ch = {}
    merged_ch = dict(local_ch)
    merged_ch.update(vps_channels)
    if merged_ch != local_ch:
        local_channels.write_text(json.dumps(merged_ch, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
    # новые каналы → строки маппинга
    existing = _folders_keys(local_folders) | _folders_keys(tmp_folders)
    additions = []
    for info in vps_channels.values():
        title = ((info or {}).get("title") or "").strip()
        if title and title.casefold() not in existing:
            additions.append(f"{title}: {slugify(title)}/")
            existing.add(title.casefold())
    if not additions:
        return 0
    block = "\n# auto-added pc_catchup (новые каналы)\n" + "\n".join(additions) + "\n"
    # локально — в конец
    with local_folders.open("a", encoding="utf-8") as f:
        f.write(block)
    # VPS — в скачанную копию и обратно (файл смонтирован, вотчер читает на каждой заметке)
    with tmp_folders.open("a", encoding="utf-8") as f:
        f.write(block)
    subprocess.run(["scp", "-i", VPS_KEY, "-o", "StrictHostKeyChecking=no",
                    str(tmp_folders), f"{VPS}:{VPS_FOLDERS}"],
                   check=True, capture_output=True, timeout=60)
    print(f"[folders] авто-маппинг добавлен: {len(additions)} "
          f"({', '.join(additions)})", flush=True)
    return len(additions)


async def enrich_media_note(client, rec, tmp_note, body):
    """Медиа-вложение → скачать (user-сессия, без лимитов) → Deepgram → секции в body.

    Возвращает (body, has_transcript).
    """
    import deepgram_lib as dg

    transcribable = {"voice", "video", "video_note", "audio", "document"}
    kinds = rec.get("media") or []
    if not (set(kinds) & transcribable):
        return body, False
    try:
        msg = await client.get_messages(rec["channel_id"], ids=rec["msg_id"])
    except Exception as e:
        print(f"  [!] медиа {rec['msg_id']}: {type(e).__name__}", flush=True)
        return body, False
    if msg is None or msg.document is None:
        return body, False
    mime = msg.document.mime_type or ""
    skip = any(type(a).__name__ in ("DocumentAttributeSticker", "DocumentAttributeAnimated")
               for a in msg.document.attributes)
    has_av = any(type(a).__name__ in ("DocumentAttributeVideo", "DocumentAttributeAudio")
                 for a in msg.document.attributes)
    if skip or not (has_av or mime.startswith(("audio/", "video/"))):
        return body, False
    tmp_dir = tmp_note / "media"
    tmp_dir.mkdir(exist_ok=True)
    try:
        f = await asyncio.wait_for(msg.download_media(file=tmp_dir), timeout=300)
    except asyncio.TimeoutError:
        print(f"  [!] скачивание {rec['msg_id']}: таймаут 300с", flush=True)
        return body, False
    except Exception as e:
        print(f"  [!] скачивание {rec['msg_id']}: {type(e).__name__}", flush=True)
        return body, False
    if not f:
        return body, False
    comp = None
    try:
        comp, _ = dg.compress_audio(Path(f))
        res = dg.transcribe_file(comp or Path(f))
        if res["status"] != "ok":
            print(f"  [!] deepgram {rec['msg_id']}: {res.get('error', '')[:80]}", flush=True)
            return body, False
        text = res["transcript"]
        summary = ""
        try:
            summary = ye.llm_summary(text, "")
        except Exception:
            pass
        v_attr = next((a for a in msg.document.attributes
                       if type(a).__name__ == "DocumentAttributeVideo"), None)
        a_attr = next((a for a in msg.document.attributes
                       if type(a).__name__ == "DocumentAttributeAudio"), None)
        dur = (getattr(v_attr, "duration", 0) if v_attr is not None
               else getattr(a_attr, "duration", 0)) or 0
        kind = ("voice" if a_attr is not None and getattr(a_attr, "voice", False)
                else "video_note" if v_attr is not None and getattr(v_attr, "round_message", False)
                else "video" if v_attr is not None
                else "audio" if a_attr is not None else "document")
        mm, ss = int(dur) // 60, int(dur) % 60
        section = (f"\n\n---\n\n## 🎧 Медиа\n\n"
                   f"- **Тип:** {kind} ({mm}:{ss:02d})\n"
                   f"- **Резюме:** {summary or '_(нет)_'}\n"
                   f"- **Транскрипт:** Deepgram Nova-3 (аудио из Telegram)\n"
                   f"\n## Транскрипт\n\n{text}\n")
        return body + section, True
    finally:
        if comp:
            Path(comp).unlink(missing_ok=True)
        Path(f).unlink(missing_ok=True)


def enrich_note(path, vid):
    entry = ye.fetch_transcript(vid)
    if entry.get("status") in ("ok", "no_captions"):
        result = ye.patch_note(path, vid, entry)
        if isinstance(result, tuple):
            path = path.parent / (result[1] + ".md")
        content = path.read_text(encoding="utf-8")
        if entry.get("status") == "ok" and "## Транскрипт" in content:
            transcript = content.split("## Транскрипт", 1)[1].strip()
            summary = ye.llm_summary(transcript, "")
            if summary:
                content = re.sub(r"- \*\*Резюме \(черновик\):\*\*.*", "- **Резюме:** " + summary,
                                 content, count=1)
                path.write_text(content, encoding="utf-8")
        elif entry.get("status") == "no_captions":
            d = yd.yt_api("videos", {"part": "snippet", "id": vid, "key": yd.yt_key()})
            desc = ""
            for item in (d or {}).get("items", []):
                desc = (item.get("snippet") or {}).get("description", "")
            comments = yd.yt_comments(vid)
            tm = re.search(r"## 📺 (.+)", content)
            guess = yd.llm_guess_summary(tm.group(1) if tm else vid, desc, comments)
            desc_r = desc.strip()
            if desc_r and not re.search(r"[а-яА-ЯёЁ]", desc_r):
                desc_r = ye.llm_translate(desc_r)
            section = "\n## Описание\n\n" + (desc_r or "_(описания нет)_")
            if guess:
                section += "\n\n**Резюме-предположение (по описанию и комментариям):** " + guess
            warn = re.search(r"- \*\*Резюме:\*\* ⚠️[^\n]*", content)
            if warn:
                content = content.replace(warn.group(0),
                                          "- **Резюме:** ⚠️ транскрипта нет (у видео нет субтитров)" + section)
                path.write_text(content, encoding="utf-8")
    return path, entry.get("status")


async def main():
    if fg.guard_or_exit(lambda m: print(m, flush=True)):
        return
    # 2026-10-21: запись в vault только при открытом Obsidian (инцидент 15–16.09).
    # Guard стоит ДО коннекта к Telegram и до любых выборок — курсор не сдвигается,
    # новые посты подхватятся в следующем прогоне.
    if not vault_guard.require_obsidian(lambda m: print(m, flush=True), "pc_catchup"):
        return
    _kill_stale_catchup()
    t_start = time.time()
    MAX_RUNTIME = 25 * 60  # 25 мин — жёсткий потолок прогона (защита от висков сети)
    import subprocess as _sp
    _ps = ('Get-CimInstance Win32_Process -Filter "Name=\'python.exe\'" | '
           'Select-Object -ExpandProperty CommandLine')
    try:
        _out = _sp.run(["powershell", "-NoProfile", "-Command", _ps],
                       capture_output=True, text=True, timeout=45)
        _stdout = _out.stdout or ""
    except Exception:
        _stdout = ""
    if "attachments_backfill" in _stdout:
        print("[skip] attachments_backfill держит Telegram-сессию — уступаю ему (безопасно, бот на VPS ловит живые посты)")
        return
    sync_folders()
    folders = load_folders()
    channels = json.loads((CODE / "channels.json").read_text(encoding="utf-8"))
    state = vps_state_download()
    env = {}
    for line in (CODE / ".env").read_text(encoding="utf-8-sig").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    session = CODE / "session" / env.get("SESSION_NAME", "vault_import")
    client = TelegramClient(str(session), int(env["API_ID"]), env["API_HASH"])
    # 2026-10-25: коннект с таймаутом (инцидент 22.10: connect без таймаута
    # пережил свой прогон на 2+ дня и заблокировал все последующие)
    try:
        await asyncio.wait_for(client.connect(), timeout=120)
    except Exception as e:
        print(f"[!] коннект к Telegram не удался за 120с ({type(e).__name__}) — "
              f"выхожу, повтор в следующем прогоне", flush=True)
        try:
            await client.disconnect()
        except Exception:
            pass
        return
    if not await client.is_user_authorized():
        print("[!] Сессия не авторизована")
        return
    total_made = total_enriched = 0
    for did_str, info in channels.items():
        if time.time() - t_start > MAX_RUNTIME:
            print(f"[!] прогон достиг потолка {MAX_RUNTIME // 60} мин — продолжу в следующем часовом прогоне", flush=True)
            break
        title = info["title"]
        last = state.get(did_str, 0)
        meta = {"channel_id": int(did_str), "title": title, "username": info.get("username")}
        ch_dir = TG_DIR / slugify(title)
        try:
            raw = await asyncio.wait_for(
                _collect_channel(client, did_str, last, meta), timeout=480)
        except asyncio.TimeoutError:
            print(f"[!] {title}: сбор сообщений завис >8 мин — пропускаю канал "
                  f"(доберёт следующий прогон, state не тронут)", flush=True)
            continue
        except Exception as e:
            if fg.is_flood_error(e):
                print(fg.set_cooldown(60, f"( flood на «{title}» )"), flush=True)
                raise fg.FloodHit()
            print(f"[!] {title}: {type(e).__name__}: {str(e)[:90]}")
            continue
        if not raw:
            continue
        raw_max = max(r["msg_id"] for r in raw)
        recs = merge_albums(raw)
        recs.sort(key=lambda r: r["msg_id"])
        made = enriched = 0
        for rec in recs:
            if not (rec.get("text") or "").strip() and not rec.get("media") and not rec.get("webpage"):
                continue
            if vault_has_msg(ch_dir, rec["msg_id"]):
                continue
            used = {p.stem.casefold() for p in ch_dir.glob("*.md")}
            key = dup_key(rec)
            yt = yt_video_ids(rec.get("text") or "", (rec.get("webpage") or {}).get("url") or "")
            tmp_note = CODE / "catchup_tmp"
            tmp_note.mkdir(exist_ok=True)
            used_tmp = {p.stem.casefold() for p in tmp_note.glob("*.md")}
            rec_local = dict(rec)
            body_fm, name, url = None, None, None
            fm, _ = build_frontmatter(rec_local, (rec.get("date") or "")[:10], folders)
            body, name, url = None, None, None
            md = render_markdown(rec.get("text") or "", rec.get("entities") or [])
            media = rec.get("media") or []
            extras = []
            if media and set(media) - {"webpage"}:
                counts = {}
                for k in media:
                    counts[k] = counts.get(k, 0) + 1
                extras.append("📎 Медиа: " + ", ".join(f"{k} ×{v}" if v > 1 else k for k, v in counts.items())
                              + " — смотрите по ссылке на оригинал")
            wp = rec.get("webpage")
            if wp and wp.get("url"):
                extras.append(f"🔗 [{wp.get('title') or wp['url']}]({wp['url']})")
            body = md
            if extras:
                body = (body + "\n\n" if body.strip() else "") + "\n".join(extras)
            if not body.strip():
                body = "_(медиа-пост без текста)_"
            body, has_media_tr = await enrich_media_note(client, rec, tmp_note, body)
            if has_media_tr:
                fm = fm.replace("tags: [telegram]",
                                "has_transcript: true\ntags: [telegram]", 1)
            from tg_convert import note_filename as nf
            name = nf(rec_local, used_tmp)
            tmp_path = tmp_note / f"{name}.md"
            tmp_path.write_text(fm + "\n\n" + body + "\n", encoding="utf-8")
            vids = yt
            if vids:
                tmp_path, _st = enrich_note(tmp_path, vids[0])
                if _st == "ok":
                    enriched += 1
            ch_dir.mkdir(parents=True, exist_ok=True)
            final = ch_dir / f"{tmp_path.stem}.md"
            n = 2
            while final.exists():
                final = ch_dir / f"{tmp_path.stem}-{n}.md"
                n += 1
            shutil_move(tmp_path, final)
            if key:
                pass
            made += 1
            total_made += 1
        state[did_str] = raw_max
        print(f"[+] {title}: новых {made} (обогащено {enriched}, до {raw_max})", flush=True)
    await client.disconnect()
    vps_state_upload(state)
    print(f"=== Итог: новых заметок {total_made} (обогащено {total_enriched}) ===")


def _kill_stale_catchup():
    """2026-10-25: стоп-защита от зомби (инцидент 22.10: зависший прогон держал
    сессию SQLite и хэндл catchup.log → все последующие прогоны молча умирали).
    Любой ЧУЖОЙ pc_catchup-процесс = зомби (задача с IgnoreNew) → убиваю."""
    try:
        ps = ('Get-CimInstance Win32_Process -Filter "Name=\'python.exe\'" | '
              'Where-Object { $_.CommandLine -match \'pc_catchup\\.py\' } | '
              'Select-Object -ExpandProperty ProcessId')
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=45).stdout or ""
        me = os.getpid()
        killed = []
        for tok in out.split():
            tok = tok.strip()
            if tok.isdigit() and int(tok) != me:
                subprocess.run(["taskkill", "/F", "/PID", tok],
                               capture_output=True, timeout=30)
                killed.append(tok)
        if killed:
            print(f"[!] убит зомби pc_catchup: PID {', '.join(killed)} "
                  f"(держал сессию/лог)", flush=True)
    except Exception as e:
        print(f"[!] zombie-check не сработал: {type(e).__name__}", flush=True)


async def _collect_channel(client, did_str, last, meta):
    """Сбор сообщений одного канала (вынесено для таймаута через wait_for)."""
    raw = []
    async for msg in client.iter_messages(int(did_str), min_id=last, limit=None):
        if msg.action is not None:
            continue
        raw.append(serialize_message(msg, meta))
    return raw


def shutil_move(src, dst):
    src.replace(dst)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except fg.FloodHit:
        print("=== выход по flood-охлаждению (сессию не жжём) ===")
