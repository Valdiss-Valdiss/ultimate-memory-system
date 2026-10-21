"""Cron-обогатитель: staging → транскрипты + LLM-резюме + описания → mirror.

Запускается по cron каждые 5 минут (flock от наложения). Идемпотентен:
- сбойные (rate_limited/error) остаются в staging, ретрай до MAX_ATTEMPTS;
- честные «нет субтитров» получают описание+комменты+предположение и уезжают;
- заметки без youtube уезжают сразу;
- заметки с медиа-файлами (voice/video/audio) ждут файл от tg_media-поллера
  (до MEDIA_WAIT циклов), затем Deepgram → транскрипт + резюме → файл удаляется.
"""
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("TG_OUT_DIR", os.environ.get("TG_STAGE_DIR", "/staging"))
os.environ.setdefault("TG_CACHE_DIR", os.environ.get("TG_CACHE_DIR", "/cache"))

import deepgram_lib as dg
import yt_descriptions as yd
import yt_enrich as ye
import tg_docs_lib as doclib
import tg_photos_lib as photolib
from tg_common import load_folders

STAGE = Path(os.environ.get("TG_STAGE_DIR", "/staging"))
MIRROR = Path(os.environ.get("MIRROR_DIR", "/mirror"))
STATE_DIR = Path(os.environ.get("TG_STATE_DIR", "/app/state"))
MAX_ATTEMPTS = 3
MEDIA_WAIT = 36  # циклов по ~5 мин = до 3 ч на загрузку файла из tg_media-очереди
DOC_MAX_ATTEMPTS = 3  # конвертация доков/фото: терминал после 3 неудач (файл на месте)

WARNING = "- **Резюме:** ⚠️ транскрипт недоступен (нет субтитров у видео — даже автоматических)"
TRANSCRIBABLE = {"voice", "video", "video_note", "audio", "document"}


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_atomic(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)  # фикс attempts.tmp: директория state может отсутствовать
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def title_from(content):
    tm = re.search(r"## 📺 (.+)", content)
    return tm.group(1) if tm else ""


def gist_of(text, max_chars=320):
    t = re.sub(r"\s+", " ", text).strip()
    out = ""
    for s in re.split(r"(?<=[.!?…])\s+", t):
        if len(out) + len(s) > max_chars and out:
            break
        out += (" " if out else "") + s
    return out or t[:max_chars]


def process_youtube_note(path, vid):
    """Транскрипт → патч → резюме/описания. Возвращает (path, status)."""
    entry = ye.fetch_transcript(vid)
    if entry.get("status") in ("ok", "no_captions"):
        ye.CACHE.mkdir(exist_ok=True)
        (ye.CACHE / f"{vid}.json").write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    result = ye.patch_note(path, vid, entry)
    if isinstance(result, tuple):
        path = path.parent / (result[1] + ".md")
    content = path.read_text(encoding="utf-8")
    status = entry.get("status")
    if status == "ok" and "## Транскрипт" in content:
        transcript = content.split("## Транскрипт", 1)[1].strip()
        summary = ye.llm_summary(transcript, "")
        if summary:
            content = re.sub(r"- \*\*Резюме \(черновик\):\*\*.*", "- **Резюме:** " + summary,
                             content, count=1)
            path.write_text(content, encoding="utf-8")
    elif status == "no_captions" and "транскрипт недоступен" in content:
        d = yd.yt_api("videos", {"part": "snippet", "id": vid, "key": yd.yt_key()})
        desc = ""
        for item in (d or {}).get("items", []):
            desc = (item.get("snippet") or {}).get("description", "")
        comments = yd.yt_comments(vid)
        guess = yd.llm_guess_summary(title_from(content), desc, comments)
        desc_r = desc.strip()
        if desc_r and not re.search(r"[а-яА-ЯёЁ]", desc_r):
            desc_r = ye.llm_translate(desc_r)
        section = "\n## Описание\n\n" + (desc_r or "_(описания нет)_")
        if guess:
            section += "\n\n**Резюме-предположение (по описанию и комментариям):** " + guess
        content = content.replace(WARNING, "- **Резюме:** ⚠️ транскрипта нет (у видео нет субтитров)" + section)
        path.write_text(content, encoding="utf-8")
    return path, status


def find_media_files(cid, mid, media_types):
    """Файлы поллера в staging/media, совпадающие с постом (msg_id или окно альбома)."""
    mdir = STAGE / "media"
    if not mdir.exists():
        return []
    n_album = max(1, len(media_types))
    out = []
    for side in mdir.rglob("c*.json"):
        try:
            sc = json.loads(side.read_text(encoding="utf-8"))
        except Exception:
            continue
        if sc.get("channel_id") != cid:
            continue
        smid = sc.get("msg_id")
        if smid == mid or (mid < smid and smid <= mid + 2 * n_album + 2):
            media_f = None
            cands = sorted(side.parent.glob(side.stem + "_*"))
            if cands:
                media_f = cands[0]
                out.append((media_f, side, sc))
    out.sort(key=lambda t: t[2].get("msg_id", 0))
    return out


def fmt_dur(sec):
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"


def process_media_note(path, content):
    """Медиа-файлы → Deepgram → секции «Медиа» + «Транскрипт» → файл удалить.

    Возвращает (path, status): ok | wait | error | no_credits.
    """
    cid = int(re.search(r"^channel_id: (-?\d+)$", content, re.M).group(1))
    mid = int(re.search(r"^msg_id: (\d+)$", content, re.M).group(1))
    tm = re.search(r"^media: \[([^\]]+)\]$", content, re.M)
    media_types = [x.strip() for x in tm.group(1).split(",")] if tm else []
    files = find_media_files(cid, mid, media_types)
    if not files:
        print(f"    [dbg] {path.name}: жду файлы staging/media/{cid}_{mid}_file_*",
              flush=True)
        return path, "wait"
    texts = []
    kinds = []
    last_err = ""
    for f, side, sc in files:
        comp, _ = dg.compress_audio(f)
        try:
            res = dg.transcribe_file(comp or f, state_path=STATE_DIR / "deepgram_state.json")
        finally:
            if comp:
                comp.unlink(missing_ok=True)
        if res["status"] == "no_deepgram_credits":
            print(f"  [dbg] {path.name}: no_credits: {res.get('error') or 'нет ключей'}; "
                  f"ключей в env: {len(dg.deepgram_keys())}", flush=True)
            return path, "no_credits"
        if res["status"] != "ok":
            last_err = res.get("error") or ""
            print(f"  [dbg] {path.name}: {sc.get('kind')}: {last_err[:150]}", flush=True)
            continue
        texts.append(res["transcript"])
        kinds.append(f"{sc.get('kind', 'медиа')} ({fmt_dur(sc.get('duration'))})")
    if not texts:
        print(f"  [dbg] {path.name}: итоговая ошибка: {last_err[:150]}", flush=True)
        return path, "error"
    combined = "\n\n".join(texts).strip()
    if not combined:
        for f, side, sc in files:
            f.unlink(missing_ok=True)
            side.unlink(missing_ok=True)
        content = path.read_text(encoding="utf-8")
        if "речей нет (музыка/заставки)" not in content:
            content = content.rstrip("\n") + ("\n\n- **Медиа:** речи нет (музыка/заставки) — "
                                              "расшифровка пуста\n")
            path.write_text(content, encoding="utf-8")
        return path, "ok"
    summary = ye.llm_summary(combined, "")
    content = path.read_text(encoding="utf-8")
    if "## Транскрипт" not in content:
        content = content.replace("tags: [telegram]", "has_transcript: true\ntags: [telegram]", 1)
        section = ("\n\n---\n\n## 🎧 Медиа\n\n"
                   f"- **Тип:** {', '.join(kinds)}\n"
                   f"- **Резюме:** {summary or gist_of(combined)}\n"
                   "- **Транскрипт:** Deepgram Nova-3 (аудио из Telegram)\n"
                   f"\n## Транскрипт\n\n{combined}\n")
        content = content.rstrip("\n") + section
        path.write_text(content, encoding="utf-8")
    for f, side, sc in files:
        f.unlink(missing_ok=True)
        side.unlink(missing_ok=True)
    return path, "ok"


def classify_sidecar(sc):
    """(is_av, is_img) по sidecar от поллера: a/v → старый путь; остальное → конвертер."""
    mime = (sc.get("mime") or "").lower()
    kind = sc.get("kind") or ""
    is_av = kind in ("voice", "video_note", "audio", "video") or mime.startswith(("audio/", "video/"))
    is_img = kind == "photo" or mime.startswith("image/")
    return is_av, is_img


def check_too_big(cid, mid):
    """Sidecar kind=too_big у поллера для (cid, mid)? → (sidecar, sc) или None."""
    mdir = STAGE / "media"
    if not mdir.exists():
        return None
    for side in mdir.rglob(f"c{abs(cid)}_{mid}.json"):
        try:
            sc = json.loads(side.read_text(encoding="utf-8"))
        except Exception:
            continue
        if sc.get("kind") == "too_big" and sc.get("channel_id") == cid:
            return side, sc
    return None


def fmt_size(n):
    n = int(n or 0)
    return f"{n // 1024 // 1024} МБ" if n >= 1024 * 1024 else f"{n // 1024} КБ"


def fm_insert(content, line):
    if line not in content:
        content = content.replace("tags: [telegram]", line + "\ntags: [telegram]", 1)
    return content


def process_attachment_note(path, content, files, tries=0):
    """Документы/фото (в т.ч. в смеси с a/v): конвертация → патч.

    Возвращает (path, status): ok | wait | conv_error | no_credits.
    Успешные части пишутся сразу (partial patch) и не дублируются на ретрае
    (guard по секциям/флагам); сбойные файлы остаются для ретрая, терминал
    после DOC_MAX_ATTEMPTS — пометка в заметке, файл удаляется.
    """
    cid = int(re.search(r"^channel_id: (-?\d+)$", content, re.M).group(1))
    mid = int(re.search(r"^msg_id: (\d+)$", content, re.M).group(1))
    av_t, doc_t, img_t = [], [], []
    for f, side, sc in files:
        is_av, is_img = classify_sidecar(sc)
        if is_av:
            av_t.append((f, side, sc))
        elif is_img:
            img_t.append((f, side, sc))
        else:
            doc_t.append((f, side, sc))
    sections = []
    flags = []

    def flush():
        if not sections:
            return
        content2 = path.read_text(encoding="utf-8")
        for fl in dict.fromkeys(flags):
            content2 = fm_insert(content2, f"{fl}: true")
        path.write_text(content2.rstrip("\n") + "".join(sections) + "\n", encoding="utf-8")

    # --- a/v-часть (та же механика, что в process_media_note) ---
    if av_t:
        texts, kinds, no_credits = [], [], False
        for f, side, sc in av_t:
            comp, _ = dg.compress_audio(f)
            try:
                res = dg.transcribe_file(comp or f, state_path=STATE_DIR / "deepgram_state.json")
            finally:
                if comp:
                    comp.unlink(missing_ok=True)
            if res["status"] == "no_deepgram_credits":
                no_credits = True
                continue
            if res["status"] != "ok":
                print(f"  [dbg] {path.name}: {sc.get('kind')}: "
                      f"{(res.get('error') or '')[:150]}", flush=True)
                continue
            texts.append(res["transcript"])
            kinds.append(f"{sc.get('kind', 'медиа')} ({fmt_dur(sc.get('duration'))})")
            f.unlink(missing_ok=True)
            side.unlink(missing_ok=True)
        combined = "\n\n".join(texts).strip()
        if combined and "## Транскрипт" not in content:
            summary = ye.llm_summary(combined, "")
            sections.append("\n\n---\n\n## 🎧 Медиа\n\n"
                            f"- **Тип:** {', '.join(kinds)}\n"
                            f"- **Резюме:** {summary or gist_of(combined)}\n"
                            "- **Транскрипт:** Deepgram Nova-3 (аудио из Telegram)\n"
                            f"\n## Транскрипт\n\n{combined}\n")
            flags.append("has_transcript")
        elif not combined:
            if no_credits:
                flush()
                return path, "no_credits"
            if "речей нет (музыка/заставки)" not in content:
                sections.append("\n\n- **Медиа:** речи нет (музыка/заставки) — расшифровка пуста\n")
            for f, side, sc in av_t:
                f.unlink(missing_ok=True)
                side.unlink(missing_ok=True)
    # --- документы ---
    for f, side, sc in doc_t:
        name = (sc.get("title") or f.name.split("_", 2)[-1]).strip() or f.name
        st, text = doclib.convert_document(f, sc.get("mime") or "",
                                           job_key=f"{cid}:{mid}:{name}")
        if st == "wait_job":
            print(f"  [dbg] {path.name}: docling job продолжает работать — ждём результата",
                  flush=True)
            return path, "wait_job"
        if st == "unsupported":
            sections.append(f"\n\n- **Документ:** ⚠️ формат {text or 'неизвестный'} не поддерживается — файл в посте\n")
            f.unlink(missing_ok=True)
            side.unlink(missing_ok=True)
            continue
        if st == "empty" or (st == "ok" and doclib.looks_like_scan(text)):
            st, text = doclib.mimo_scan_text(f, sc.get("mime") or "")
        if st == "ok":
            summary = doclib.doc_summary(text) or gist_of(text)
            sections.append("\n\n---\n\n"
                            f"## 📄 Документ: {name}\n\n"
                            f"- **Файл:** {name} ({fmt_size(sc.get('size'))})\n"
                            f"- **Резюме документа:** {summary or gist_of(text)}\n"
                            f"\n## Текст документа\n\n{doclib.cap_text(text)}\n")
            flags.append("has_doc_text")
            f.unlink(missing_ok=True)
            side.unlink(missing_ok=True)
        elif tries + 1 >= DOC_MAX_ATTEMPTS:
            sections.append(f"\n\n- **Документ:** ⚠️ не обработан ({str(text)[:120]}) — файл в посте\n")
            f.unlink(missing_ok=True)
            side.unlink(missing_ok=True)
        else:
            print(f"  [dbg] {path.name}: документ {name}: {st}: {str(text)[:120]}", flush=True)
            flush()
            return path, "conv_error"
    # --- фото ---
    idx = len(re.findall(r"^## 🖼 Изображение", content, re.M))
    for f, side, sc in img_t:
        res, err = photolib.analyze_photo(f, sc.get("mime") or "image/jpeg")
        if err:
            if tries + 1 >= DOC_MAX_ATTEMPTS:
                sections.append(f"\n\n- **Изображение:** ⚠️ не обработано ({str(err)[:100]}) — файл в посте\n")
                f.unlink(missing_ok=True)
                side.unlink(missing_ok=True)
                continue
            flush()
            print(f"  [dbg] {path.name}: фото: {str(err)[:120]}", flush=True)
            return path, "conv_error"
        idx += 1
        if res["meaningful"]:
            parts = [f"\n\n---\n\n## 🖼 Изображение ({idx})\n\n",
                     f"- **Тип:** {res['type']}\n"]
            if res["description"]:
                parts.append(f"- **Описание:** {res['description']}\n")
            if res["text"]:
                parts.append(f"- **Текст на изображении:**\n\n{res['text']}\n")
            sections.append("".join(parts))
            flags.append("has_ocr_text")
        else:
            sections.append("\n\n- **Изображение:** текстовой нагрузки нет (мем/фото) — пропуск\n")
        f.unlink(missing_ok=True)
        side.unlink(missing_ok=True)
    flush()
    return path, "ok"


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    folders = load_folders()
    if not STAGE.exists():
        print("staging пуст — выход", flush=True)
        return
    attempts = load_json(STATE_DIR / "attempts.json", {})
    notes = sorted(p for p in STAGE.rglob("*.md"))
    print(f"заметок в staging: {len(notes)}", flush=True)
    moved = enriched = waited = 0
    for p in notes:
        rel = p.relative_to(STAGE)
        key = str(rel)
        content = p.read_text(encoding="utf-8")
        m = re.search(r"^youtube: https://youtu\.be/([\w\-]+)$", content, re.M)
        status = "skip"
        media_m = re.search(r"^media: \[([^\]]+)\]$", content, re.M)
        media_types = [x.strip() for x in media_m.group(1).split(",")] if media_m else []
        if not m and media_types:
            cid = int(re.search(r"^channel_id: (-?\d+)$", content, re.M).group(1))
            mid = int(re.search(r"^msg_id: (\d+)$", content, re.M).group(1))
            files = find_media_files(cid, mid, media_types)
            has_new = any(not classify_sidecar(sc)[0] for _, _, sc in files)
            if has_new:
                # документы и/или фото (в т.ч. в смеси с a/v) — конвертер
                tries = attempts.get(key, 0)
                p, status = process_attachment_note(p, content, files, tries)
                if status == "wait_job":
                    waited += 1
                    print(f"  [wait] {key}: docling job идёт на сервере — результат заберём "
                          "в следующем цикле", flush=True)
                    continue
                if status in ("conv_error", "no_credits"):
                    attempts[key] = tries + 1
                    if status == "no_credits" or attempts[key] < DOC_MAX_ATTEMPTS:
                        waited += 1
                        print(f"  [wait] {key}: вложения {status} ({attempts[key]})",
                              flush=True)
                        continue
                    status = "gave-up"  # терминальные пометки уже в заметке
                else:
                    enriched += 1
                    print(f"  [attach] {key}: {status}", flush=True)
            elif files and set(media_types) & TRANSCRIBABLE and "## Транскрипт" not in content:
                # чистый a/v-путь — поведение не менялось
                tries = attempts.get(key, 0)
                p, status = process_media_note(p, content)
                if status in ("wait", "error", "no_credits"):
                    attempts[key] = tries + 1
                    if attempts[key] < MEDIA_WAIT:
                        waited += 1
                        print(f"  [wait] {key}: медиа {status} ({attempts[key]}/{MEDIA_WAIT})",
                              flush=True)
                        continue
                    for f, side, sc in find_media_files(cid, mid, media_types):
                        f.unlink(missing_ok=True)
                        side.unlink(missing_ok=True)
                    content = p.read_text(encoding="utf-8")
                    content = content.rstrip("\n") + ("\n\n- **Медиа:** ⚠️ файл не получен — "
                                                      "транскрибация пропущена\n")
                    p.write_text(content, encoding="utf-8")
                    status = "gave-up"
                else:
                    enriched += 1
                    print(f"  [media] {key}: {status}", flush=True)
            elif not files:
                tb = check_too_big(cid, mid)
                if tb:
                    side, sc = tb
                    mb = (sc.get("size") or 0) // 1024 // 1024
                    content = content.rstrip("\n") + (
                        f"\n\n- **Документ:** ⚠️ файл слишком большой ({mb} МБ) — "
                        "обработка пропущена\n")
                    p.write_text(content, encoding="utf-8")
                    side.unlink(missing_ok=True)
                    status = "ok"
                    enriched += 1
                    print(f"  [too-big] {key}: закрыт", flush=True)
                else:
                    tries = attempts.get(key, 0)
                    attempts[key] = tries + 1
                    if attempts[key] < MEDIA_WAIT:
                        waited += 1
                        print(f"  [wait] {key}: жду файл поллера ({attempts[key]}/{MEDIA_WAIT})",
                              flush=True)
                        continue
                    content = content.rstrip("\n") + ("\n\n- **Медиа:** ⚠️ файл не получен — "
                                                      "транскрибация пропущена\n")
                    p.write_text(content, encoding="utf-8")
                    status = "gave-up"
            else:
                status = "skip"  # a/v-файлы, но секция уже есть — просто уезжает
        elif m:
            vid = m.group(1)
            tries = attempts.get(key, 0)
            if "## 📺" in content:
                if "нет субтитров" in content:
                    status = "final"  # честное «нет субтитров» — уезжает как есть
                elif tries >= MAX_ATTEMPTS:
                    status = "gave-up"  # исчерпаны — уезжает с пометкой
                else:
                    ye.revert_failed_note(p)
                    p, status = process_youtube_note(p, vid)
            else:
                p, status = process_youtube_note(p, vid)
            if status in ("rate_limited", "error"):
                attempts[key] = tries + 1
                if attempts[key] < MAX_ATTEMPTS:
                    waited += 1
                    print(f"  [wait] {key}: {status} ({attempts[key]}/{MAX_ATTEMPTS}) — остаётся в staging", flush=True)
                    continue
        content = p.read_text(encoding="utf-8")
        if "folder:" not in content.split("---", 2)[1]:
            tm = re.search(r'^channel: "(.+)"$', content, re.M)
            target = folders.get(tm.group(1)) if tm else None
            if target:
                content = content.replace("tags: [telegram]", f"folder: \"{target}\"\ntags: [telegram]", 1)
        p.write_text(content, encoding="utf-8")
        rel = p.relative_to(STAGE)
        dest = MIRROR / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dest))
        moved += 1
        if m:
            print(f"  [done] {key}: {status}", flush=True)
    save_atomic(STATE_DIR / "attempts.json", attempts)
    print(f"итог: перемещено {moved}, YT-обогащено {enriched}, ждут ретрая {waited}", flush=True)


if __name__ == "__main__":
    main()
