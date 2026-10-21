# -*- coding: utf-8 -*-
"""Re-summary: догенерация пустых резюме документов (mimo) БЕЗ перескачивания/реконвертации.

Текст берётся прямо из заметки (секция «## Текст документа»), резюме заменяет «_нет_».
Запуск (ПК): py -3 re_summary.py [--limit N]
Защита: Obsidian должен быть открыт; паузы между вызовами (LLM_PAUSE_SEC, default 20).
Урок 2026-09-14: 48 резюме потеряно ночью троттлинга VPS (doc_summary глотал ошибки).
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import tg_docs_lib as doclib  # noqa: E402  (doc_summary с ретраями и stderr-логом)

VAULT = Path(os.environ.get("VAULT", r"C:\Users\<user>\Documents\Obsidian Vault"))
LOG = SCRIPT_DIR / "re_summary.log"
LLM_PAUSE_SEC = int(os.environ.get("LLM_PAUSE_SEC", "20"))
BATCH = 10
BATCH_PAUSE_SEC = 90
SEC_RE = re.compile(r"(## 📄 Документ: [^\n]+)\n\n- \*\*Файл:\*\* [^\n]*\n- \*\*Резюме документа:\*\* (_нет_|)\n\n## Текст документа\n\n", re.S)
HDR = "## 📄 Документ: "


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def doc_text_segment(seg):
    """Текст документа из сегмента секции (до конца сегмента)."""
    return seg


def scan_notes():
    out = []
    for p in VAULT.glob("**/*.md"):
        if ".obsidian" in p.parts or "backups" in p.parts:
            continue
        try:
            s = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if "_нет_" in s and "## Текст документа" in s:
            for m in SEC_RE.finditer(s):
                if m.group(2) == "_нет_":
                    out.append((p, m.start()))
    return out


def patch_note(path):
    """Патчит ВСЕ пустые резюме в заметке. Возвращает (ok, failed, err)."""
    s = path.read_text(encoding="utf-8")
    matches = list(SEC_RE.finditer(s))
    if not matches:
        return 0, 0, ""
    parts, pos, ok = [], 0, 0
    fail = 0
    err = ""
    for i, m in enumerate(matches):
        seg_start, seg_end = m.start(), (matches[i + 1].start() if i + 1 < len(matches) else len(s))
        if m.group(2) != "_нет_":
            continue
        seg = s[seg_start:seg_end]
        text = seg.split("## Текст документа\n\n", 1)[1].strip()
        if not text or len(text) < 200:
            fail += 1
            err = f"text too short ({len(text)})"
            continue
        summary = doclib.doc_summary(text)
        if summary:
            new_seg = seg.replace(
                "- **Резюме документа:** _нет_",
                f"- **Резюме документа:** {summary.strip()}", 1)
            parts.append((seg_start, seg_end, new_seg))
            ok += 1
        else:
            fail += 1
            err = "summary empty after retries"
    if not parts:
        return 0, fail, err
    parts.sort()
    buf, pos = [], 0
    for a, b, t in parts:
        buf.append(s[pos:a]); buf.append(t); pos = b
    buf.append(s[pos:])
    path.write_text("".join(buf), encoding="utf-8")
    return ok, fail, err


def main():
    running = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Obsidian.exe"],
                               capture_output=True, text=True,
                               encoding="cp1251", errors="replace").stdout.lower().find("obsidian.exe")
    if running == -1:
        log("[!] Obsidian не запущен — внешние записи в vault запрещены. Открой Obsidian.")
        return
    if not doclib.ye.llm_key():
        log("[!] Нет LLM-ключа (LLM_KEY в .env или auth.json)")
        return
    hits = scan_notes()
    notes = sorted({p for p, _ in hits})
    log(f"=== re-summary: {len(hits)} пустых резюме в {len(notes)} заметках ===")
    limit = 0
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
        notes = notes[:limit]
        log(f"лимит запуска: {limit}")
    total_ok = total_fail = 0
    for i, p in enumerate(notes, 1):
        try:
            ok, fail, err = patch_note(p)
            total_ok += ok
            total_fail += fail
            log(f"[{'+' if ok else '-'}] {p.name}: резюме {ok}, пропусков {fail}"
                + (f", err={err}" if err else "") + f" [{i}/{len(notes)}]")
        except Exception as e:
            total_fail += 1
            log(f"[err] {p.name}: {type(e).__name__}: {str(e)[:120]}")
        if i < len(notes):
            time.sleep(LLM_PAUSE_SEC)
            if i % BATCH == 0:
                log(f"[pace] батч {i // BATCH} — пауза {BATCH_PAUSE_SEC} с")
                time.sleep(BATCH_PAUSE_SEC)
    log(f"=== Итог: резюме {total_ok}, пропусков/ошибок {total_fail} ===")


if __name__ == "__main__":
    main()
