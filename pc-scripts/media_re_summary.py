# -*- coding: utf-8 -*-
"""Догенерация пустых резюме в МЕДИА-заметках (видео/аудио).

re_summary.py умеет только документы («Резюме документа»). Медиа-формат
(«- **Резюме:** _(нет)_») оставался пустым, если LLM был недоступен в момент
обработки (как 19–20.10, когда ключи провайдера истекли).

Скрипт идемпотентен: берёт транскрипт из той же заметки, генерирует резюме
через общий LLM-ротатор и заменяет строку «- **Резюме:** _(нет)_».

Запуск: python media_re_summary.py [--limit N] [--dry]
"""
import argparse
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
VAULT = Path(os.environ.get("VAULT_DIR", r"C:\Users\<user>\Documents\Obsidian Vault"))

EMPTY_SUMMARY = "- **Резюме:** _(нет)_"
TRANSCRIPT_RE = re.compile(r"^## 🎧 Медиа\s*$", re.M)
TRANSCRIPT_BLOCK_RE = re.compile(r"^## Транскрипт\s*\n(.*?)(?=^## |\Z)", re.M | re.S)

import vault_guard  # noqa: E402


def find_targets(limit=0):
    out = []
    for p in VAULT.rglob("*.md"):
        try:
            t = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if EMPTY_SUMMARY not in t or not TRANSCRIPT_RE.search(t):
            continue
        m = TRANSCRIPT_BLOCK_RE.search(t)
        tr = (m.group(1) if m else "").strip()
        if len(tr) < 200:
            # Видео без речи: транскрипта нет → резюме строим по тексту поста
            body = t.split("## 🎧 Медиа")[0]
            body = re.sub(r"^---.*?---", "", body, flags=re.S).strip()
            tr = body
            if len(tr) < 200:
                continue
        out.append((p, tr))
    if limit:
        out = out[:limit]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    if not args.dry and not vault_guard.require_obsidian(print, "media_re_summary"):
        return
    import yt_enrich as ye
    targets = find_targets(args.limit)
    print(f"=== media_re_summary: кандидатов {len(targets)} ===")
    done = fail = 0
    for p, transcript in targets:
        try:
            summary = ye.llm_summary(transcript, "")
        except Exception as e:
            print(f"[err] {p.name}: {type(e).__name__}: {str(e)[:80]}", flush=True)
            fail += 1
            continue
        if not summary:
            print(f"[err] {p.name}: LLM не вернул резюме", flush=True)
            fail += 1
            continue
        if args.dry:
            print(f"[dry] {p.name} → {summary[:100]}")
            continue
        t = p.read_text(encoding="utf-8")
        t = t.replace(EMPTY_SUMMARY, f"- **Резюме:** {summary}", 1)
        p.write_text(t, encoding="utf-8")
        done += 1
        print(f"[+] {p.name} — резюме добавлено", flush=True)
    print(f"=== Итог: резюме {done}, ошибок {fail} ===")


if __name__ == "__main__":
    main()
