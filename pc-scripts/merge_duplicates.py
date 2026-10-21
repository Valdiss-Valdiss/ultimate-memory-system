"""Слияние дублей: Сохраненное/Telegram/<канал>/*.md vs целевые папки.

Если заметка есть в обоих местах — в целевую пишется БОГАТАЯ версия (с транскриптом),
копия в Сохраненное удаляется (LiveSync разнесёт удаление на mirror/телефон).
"""
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

VAULT = Path(r"C:\Users\<user>\Documents\Obsidian Vault")
ROOT = VAULT / "Inbox" / "Hermes" / "Сохраненное" / "Telegram"
TRANSCRIBABLE = {"voice", "video", "video_note", "audio", "document"}


def fm_str(text, key):
    m = re.search(rf'^{key}: "?(.+?)"?\s*$', text, re.M)
    return m.group(1).strip() if m else None


def richness(c):
    score = 0
    if "## Транскрипт" in c:
        score += 100
    if "## 📺" in c:
        score += 10
    if "## Описание" in c:
        score += 5
    return score + len(c) // 10000


merged = deleted = moved = 0
for ch_dir in sorted(ROOT.iterdir()):
    if not ch_dir.is_dir():
        continue
    for p in sorted(ch_dir.glob("*.md")):
        if p.name == "_index.md":
            continue
        content = p.read_text(encoding="utf-8", errors="replace")
        fm = content[:2000]
        target_rel = fm_str(fm, "folder")
        if not target_rel:
            continue
        dest = VAULT / target_rel / p.name
        if dest.exists():
            dcontent = dest.read_text(encoding="utf-8", errors="replace")
            if richness(content) > richness(dcontent):
                dest.write_text(content, encoding="utf-8")
                merged += 1
                print(f"[merge] {target_rel}/{p.name} ← версия из Сохраненного (богаче)")
            p.unlink()
            deleted += 1
        else:
            dest_dir = VAULT / target_rel
            dest_dir.mkdir(parents=True, exist_ok=True)
            p.rename(dest)
            moved += 1
            print(f"[move] {ch_dir.name}/{p.name} → {target_rel}")
print(f"=== слито {merged}, удалено дублей {deleted}, перенесено {moved} ===")
