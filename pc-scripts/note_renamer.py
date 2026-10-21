# -*- coding: utf-8 -*-
"""note_renamer: переименование безымянных заметок по содержимому фото/документа.

Ищет заметки с авто-именами миграции (без-названия*, document*, imagex*, photo*)
у которых есть осмысленное содержимое (Описание фото от mimo / Резюме документа)
и генерирует слаг ≤60 символов. Пилот: --limit N. Полный прогон: --execute.
Защита: Obsidian должен быть открыт; коллизии имени → суффикс msg_id;
backlinks не трогаем (LiveSync/индексер перестроят пути).
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

VAULT = Path(os.environ.get("VAULT_DIR", r"C:\Users\<user>\Documents\Obsidian Vault"))
LOG = Path(__file__).resolve().parent / "note_renamer.log"
PATTERNS = re.compile(r"^(без-названия|document|imagex|photo)", re.I)
STOP = {"в", "и", "с", "на", "для", "от", "по", "из", "о", "об", "это", "как", "что", "у", "к", "а", "но", "да"}
BAD = r"\/:*?\"<>|"


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)
    print(line, flush=True)
    with open(Path(__file__).resolve().parent / "note_renamer.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def slug_from(text):
    t = re.sub(r"[^\w\s-]", " ", text)
    t = re.sub(r"\s+", " ", t).strip()
    words = [w for w in t.split() if w.lower() not in STOP]
    while words and words[0].isdigit():
        words = words[1:]
    slug = "-".join(words)
    if len(slug) > 60:
        slug = slug[:60]
        if slug.rfind("-") > 30:
            slug = slug[:slug.rfind("-")]
    return slug.strip("-")


def build_slug(s, title, mid):
    m = re.search(r"^- \*\*Описание:\*\* (.+)$", s, re.M)
    if m:
        slug = slug_from(m.group(1))
        if len(slug) >= 8:
            return slug
    m = re.search(r"^- \*\*Текст на изображении:\*\*\s*\n(.+?)(?:\n\n|\Z)", s, re.S | re.M)
    if m:
        first = m.group(1).strip().splitlines()[0][:120]
        slug = slug_from(first)
        if len(slug) >= 8:
            return slug
    m = re.search(r"^- \*\*Резюме документа:\*\* (.+)$", s, re.M)
    if m and m.group(1).strip() not in ("_нет_", ""):
        slug = slug_from(m.group(1))
        if len(slug) >= 8:
            return slug
    return ""


def dedupe():
    """Дедупликация: авто-имя, чей переименованный «двойник» уже существует в папке
    и содержимое байт-в-байт совпадает → старый файл удаляется. Отличия/нет двойника → пропуск."""
    plog = Path(__file__).resolve().parent / "photos_backfill.log"
    if plog.exists() and time.time() - plog.stat().st_mtime < 180:
        log("[!] бэкфилл фото активно (лог свежее 3 мин) — гонка путей. Останови бэкфилл и повтори.")
        return
    cands = []
    for p in VAULT.rglob("*.md"):
        rel = str(p.relative_to(VAULT)).replace("\\", "/")
        if "/.trash" in rel or rel.startswith(".trash/") or p.name.startswith("_index"):
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2})-(без-названия(?:-?\d+)?|documents?(?:-?\d+|x\d+)?|imagex?\d*(?:-\d+)?|photos?(?:-?\d+)?|photox?\d*(?:-\d+)?)\.md$", p.name, re.I)
        if not m:
            continue
        try:
            s = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        fm = re.search(r"^---\n(.*?)^---", s, re.S | re.M)
        if not fm:
            continue
        f = fm.group(1)
        mid = re.search(r"^msg_id: (\d+)$", f, re.M)
        created = re.search(r"^created: (\d{4}-\d{2}-\d{2})$", f, re.M)
        if not mid or not created:
            continue
        src = build_slug(s, f, p.name)
        if not src:
            continue
        target = p.parent / f"{created.group(1)}-{src}.md"
        if not target.exists() or target == p:
            continue
        cands.append((p, target))
    log(f"=== note_renamer DEDUPE: дубликатов с двойником {len(cands)} ===")
    deleted = skipped = 0
    for p, t in cands:
        try:
            if p.read_bytes() != t.read_bytes():
                skipped += 1
                log(f"[diff] {p.name[:44]} ≠ {t.name[:44]} — содержимое различается, НЕ удаляю (ручной разбор)")
                continue
            p.unlink()
            deleted += 1
            log(f"[del] дубль удалён: {p.name[:60]} (двойник: {t.name[:60]})")
        except Exception as e:
            skipped += 1
            log(f"[err] {p.name[:44]}: {type(e).__name__}: {str(e)[:80]}")
    log(f"=== Итог дедупликации: удалено {deleted}, пропущено {skipped} ===")


def main():
    if "--dedupe" in sys.argv:
        dedupe()
        return
    execute = "--execute" in sys.argv
    dry = not execute
    running = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Obsidian.exe"],
                               capture_output=True, text=True,
                               encoding="cp1251", errors="replace").stdout.lower().find("obsidian.exe")
    if running == -1:
        log("[!] Obsidian не запущен — переименование запрещено.")
        return
    if not dry:
        plog = Path(__file__).resolve().parent / "photos_backfill.log"
        if plog.exists() and time.time() - plog.stat().st_mtime < 180:
            log("[!] бэкфилл фото активно (лог свежее 3 мин) — гонка путей. Останови бэкфилл и повтори.")
            return
    renames = []
    for p in VAULT.rglob("*.md"):
        rel = str(p.relative_to(VAULT)).replace("\\", "/")
        if "/.trash" in rel or rel.startswith(".trash/") or p.name.startswith("_index"):
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2})-(без-названия(?:-?\d+)?|documents?(?:-?\d+|x\d+)?|imagex?\d*(?:-\d+)?|photos?(?:-?\d+)?|photox?\d*(?:-\d+)?)\.md$", p.name, re.I)
        if not m:
            continue
        try:
            s = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        fm = re.search(r"^---\n(.*?)^---", s, re.S | re.M)
        if not fm:
            continue
        f = fm.group(1)
        mid = re.search(r"^msg_id: (\d+)$", f, re.M)
        created = re.search(r"^created: (\d{4}-\d{2}-\d{2})$", f, re.M)
        if not mid or not created:
            continue
        src = build_slug(s, f, p.name)
        if not src:
            continue
        new_name = f"{created.group(1)}-{src}.md"
        if p.name.lower() == new_name.lower():
            continue
        target = p.parent / new_name
        if target.exists():
            new_name = f"{created.group(1)}-{src}-{mid.group(1)}.md"
            target = p.parent / new_name
            if target.exists():
                continue
        renames.append((p, target, src))
    log(f"=== note_renamer {'EXECUTE' if execute else 'DRY-RUN'}: к переименованию {len(renames)} ===")
    limit = 0
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
        renames = renames[:limit]
        log(f"лимит: {limit}")
    if dry:
        for p, t, src in renames[:80]:
            log(f"  [dry] {p.name[:48]}  →  {t.name[:48]}")
        log("=== dry-run: применение с --execute ===")
        return
    ok = fail = 0
    for p, t, src in renames:
        try:
            p.rename(t)
            ok += 1
            log(f"[+] {p.name[:44]} → {t.name[:44]}")
        except Exception as e:
            fail += 1
            log(f"[err] {p.name[:44]}: {type(e).__name__}: {str(e)[:80]}")
    log(f"=== Итог: переименовано {ok}, ошибок {fail} ===")


if __name__ == "__main__":
    main()
