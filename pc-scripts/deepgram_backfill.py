"""Backfill: YT-видео без субтитров → аудио (yt-dlp) → Deepgram → транскрипт + LLM-резюме в заметке.

Запуск: python deepgram_backfill.py [--limit N]
Пропускает: недоступные с YouTube (помечает unavailable), уже сделанные (state).
Резюм-стейт: deepgram_backfill_state.json. Аудио качается во временную папку и удаляется.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import deepgram_lib as dg
import yt_enrich as ye

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CODE = Path(__file__).resolve().parent
VAULT = Path(r"C:\Users\<user>\Documents\Obsidian Vault")
CACHE = CODE / "transcripts"
STATE = CODE / "deepgram_backfill_state.json"
TARGETS = CODE / "backfill_targets.json"
TMP = CODE / "dg_audio_tmp"
TMP.mkdir(exist_ok=True)
TODAY = time.strftime("%Y-%m-%d")


def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(s):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE)


def build_targets():
    """vid → актуальный путь заметки (свежий скан по всему vault — заметки переносятся триажем)."""
    targets = {}
    for p in VAULT.rglob("*.md"):
        if p.name == "_index.md":
            continue
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:3000]
        except OSError:
            continue
        if "## 📺" not in head or "## Транскрипт" in head:
            continue
        m = re.search(r"- \*\*Видео:\*\* https://youtu\.be/([A-Za-z0-9_-]{6,15})", head)
        if m:
            targets.setdefault(m.group(1), str(p))
    return targets


def note_path_for(vid, targets):
    p = Path(targets.get(vid, ""))
    if not p.exists():
        return None
    content = p.read_text(encoding="utf-8", errors="replace")[:3000]
    if "## 📺" not in content or "## Транскрипт" in content:
        return None
    return p


def gist_of(text, max_chars=320):
    t = re.sub(r"\s+", " ", text).strip()
    out = ""
    for s in re.split(r"(?<=[.!?…])\s+", t):
        if len(out) + len(s) > max_chars and out:
            break
        out += (" " if out else "") + s
    return out or t[:max_chars]


def ytdlp_meta(vid):
    r = subprocess.run(
        ["yt-dlp", "--dump-single-json", "--skip-download", "--no-playlist",
         "--no-warnings", "--socket-timeout", "30", f"https://youtu.be/{vid}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=150)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout or "")[-200:]
    try:
        return json.loads(r.stdout), None
    except Exception as e:
        return None, f"json: {e}"


def ytdlp_audio(vid):
    r = subprocess.run(
        ["yt-dlp", "-f", "bestaudio", "-N", "3", "--no-playlist", "--no-warnings",
         "--no-progress", "--socket-timeout", "30", "-o", str(TMP / f"{vid}.%(ext)s"),
         f"https://youtu.be/{vid}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
    files = [f for f in TMP.glob(f"{vid}.*") if f.suffix.lower() not in (".part", ".tmp")]
    if r.returncode != 0 or not files:
        for f in files:
            f.unlink(missing_ok=True)
        return None
    return files[0]


def patch_with_transcript(path, text, duration_sec):
    content = path.read_text(encoding="utf-8")
    content = re.sub(r"^has_transcript: false\b.*$", "has_transcript: true",
                     content, count=1, flags=re.M)
    repl = (f"- **Резюме (черновик):** {gist_of(text)}\n"
            f"- **Транскрипт:** Deepgram Nova-3 (аудио, {duration_sec:.0f} сек)")
    content = re.sub(r"- \*\*Резюме:\*\* ⚠️ транскрипт[^\n]*",
                     lambda m: repl, content, count=1)
    if "## Транскрипт" not in content:
        content = content.rstrip("\n") + f"\n\n---\n\n## Транскрипт\n\n{text}\n"
    path.write_text(content, encoding="utf-8")


def llm_finalize(path):
    content = path.read_text(encoding="utf-8")
    if "Резюме (черновик)" in content and "## Транскрипт" in content:
        transcript = content.split("## Транскрипт", 1)[1].strip()
        summary = ye.llm_summary(transcript, "")
        if summary:
            content = re.sub(r"- \*\*Резюме \(черновик\):\*\*.*",
                             lambda m: "- **Резюме:** " + summary, content, count=1)
            path.write_text(content, encoding="utf-8")
    tm = re.search(r"## 📺 (.+)", content)
    if tm:
        np = ye.rename_if_url_named(path, tm.group(1))
        if np is not None:
            return np
    return path


def update_cache(vid, meta, res):
    entry = {
        "status": "ok",
        "transcript": [{"text": res["transcript"], "start": 0,
                        "duration": res["duration_sec"]}],
        "language": "auto (deepgram)",
        "source": "deepgram",
        "fetched": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "metadata": {"title": (meta or {}).get("title", ""),
                     "author": (meta or {}).get("uploader", ""),
                     "duration": (meta or {}).get("duration", 0)},
    }
    (CACHE / f"{vid}.json").write_text(json.dumps(entry, ensure_ascii=False),
                                       encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    print("Сканирую vault (актуальные пути заметок)...")
    targets = build_targets()
    print(f"Без транскрипта: {len(targets)}")
    state = load_state()
    todo = []
    for vid, p in targets.items():
        st = state.get(vid, {}).get("status")
        if st in ("done", "unavailable"):
            continue
        if st == "error" and state[vid].get("tries", 0) >= 2:
            continue
        if note_path_for(vid, targets) is None:
            continue
        todo.append(vid)
    todo.sort()
    if args.limit:
        todo = todo[:args.limit]
    spent = sum(v.get("cost", 0) for v in state.values())
    print(f"Целей: {len(todo)} | уже потрачено: ${spent:.2f}")
    made = unavail = errors = 0
    for i, vid in enumerate(todo, 1):
        st = state.get(vid, {})
        tries = st.get("tries", 0) if st.get("status") == "error" else 0
        path = note_path_for(vid, targets)
        if path is None:
            state[vid] = {"status": "note_missing", "at": TODAY}
            save_state(state)
            continue
        meta, err = ytdlp_meta(vid)
        if meta is None:
            unavail += 1
            state[vid] = {"status": "unavailable", "err": (err or "")[-120:], "at": TODAY}
            save_state(state)
            print(f"[{i}/{len(todo)}] {vid}: видео недоступно с YouTube — пропущено")
            continue
        audio = ytdlp_audio(vid)
        if audio is None:
            errors += 1
            state[vid] = {"status": "error", "err": "yt-dlp download", "tries": tries + 1,
                          "at": TODAY}
            save_state(state)
            print(f"[{i}/{len(todo)}] {vid}: скачивание не удалось")
            continue
        comp, did_compress = dg.compress_audio(audio)
        to_send = comp or audio
        try:
            res = dg.transcribe_file(to_send)
        finally:
            if comp:
                comp.unlink(missing_ok=True)
            audio.unlink(missing_ok=True)
        if res["status"] == "ok":
            patch_with_transcript(path, res["transcript"], res["duration_sec"])
            final = llm_finalize(path)
            update_cache(vid, meta, res)
            made += 1
            spent += res["cost_usd"]
            state[vid] = {"status": "done", "cost": res["cost_usd"],
                          "dur": res["duration_sec"], "at": TODAY}
            save_state(state)
            print(f"[{i}/{len(todo)}] {vid}: ok ({res['duration_sec']:.0f}с, "
                  f"${res['cost_usd']:.3f}) → {final.name}")
        elif res["status"] == "no_deepgram_credits":
            print("[!] Кредиты Deepgram исчерпаны — стоп. Добавьте Deepgram_api_2 в .env")
            break
        else:
            errors += 1
            state[vid] = {"status": "error", "err": (res.get("error") or "")[:120],
                          "tries": tries + 1, "at": TODAY}
            save_state(state)
            print(f"[{i}/{len(todo)}] {vid}: ошибка {res.get('error', '')[:80]}")
            time.sleep(5)
        time.sleep(2)
    print(f"=== Итог: сделано {made}, недоступно {unavail}, ошибок {errors}; "
          f"потрачено ${spent:.2f} (суммарно) ===")


if __name__ == "__main__":
    main()
