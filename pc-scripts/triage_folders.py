"""Триаж: перенос заметок из Сохраненное/Telegram/<канал>/ в целевые папки по свойству folder.

Запуск: python triage_folders.py [--dry]
- folder берётся из frontmatter (100% заметок); нет → резолв по folders.yaml → иначе пропуск.
- Физический перенос (имена сохраняются → wikilinks не ломаются, Obsidian ищет по имени).
- Коллизии имён в целевой папке → суффикс -<msg_id>.
- Индексы каналов (_index.md) остаются на месте. Идемпотентен.
"""
import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tg_common import load_folders, slugify
import vault_guard

VAULT = Path(r"C:\Users\<user>\Documents\Obsidian Vault")
ROOT = VAULT / "Inbox" / "Hermes" / "Сохраненное" / "Telegram"


def fm_str(text, key):
    m = re.search(rf'^{key}: "?(.+?)"?\s*$', text, re.M)
    return m.group(1).strip() if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    # 2026-10-21: перенос файлов = внешняя запись в vault → только при открытом
    # Obsidian (инцидент 15–16.09). Закрыт Obsidian — ничего не двигаем.
    if not args.dry and not vault_guard.require_obsidian(print, "triage"):
        return
    folders = load_folders()
    moved = skipped_nofolder = collisions = 0
    for ch_dir in sorted(ROOT.iterdir()):
        if not ch_dir.is_dir():
            continue
        for p in sorted(ch_dir.glob("*.md")):
            if p.name == "_index.md":
                continue
            content = p.read_text(encoding="utf-8", errors="replace")
            fm = content[:2000]
            target_rel = fm_str(fm, "folder")
            ch = fm_str(fm, "channel")
            if not target_rel and ch:
                target_rel = folders.get(ch) or folders.get(ch.strip('"'))
            if not target_rel and ch:
                # Фолбэк: неизвестный канал → корневая папка по имени канала
                target_rel = slugify(ch.strip('"')) + "/"
            if not target_rel:
                skipped_nofolder += 1
                print(f"[!] нет folder: {p.name}")
                continue
            dest_dir = VAULT / target_rel
            if not dest_dir.exists():
                dest_dir.mkdir(parents=True, exist_ok=True)
            msg_id = fm_str(fm, "msg_id") or "x"
            dest = dest_dir / p.name
            if dest.exists():
                # Та же заметка уже разложена (дубль по msg_id) — снимаем копию,
                # близнецов с суффиксами не создаём.
                try:
                    dest_head = dest.read_text(encoding="utf-8", errors="replace")[:1500]
                    m_dest = re.search(r"^msg_id: (\d+)$", dest_head, re.M)
                    if msg_id != "x" and m_dest and m_dest.group(1) == msg_id:
                        collisions += 1
                        if not args.dry:
                            p.unlink()
                        continue
                except OSError:
                    pass
            stem, suffix = p.stem, p.suffix
            n = 2
            while dest.exists():
                collisions += 1
                dest = dest_dir / f"{stem}-{msg_id}{suffix}" if n == 2 else \
                    dest_dir / f"{stem}-{msg_id}-{n}{suffix}"
                n += 1
            if args.dry:
                print(f"[dry] {ch_dir.name}/{p.name} → {target_rel}")
                moved += 1
                continue
            try:
                p.rename(dest)
                moved += 1
            except OSError as e:
                print(f"[!] не смог {p.name}: {e}")
    mode = "DRY" if args.dry else "OK"
    print(f"=== [{mode}] перенесено {moved}, без folder {skipped_nofolder}, "
          f"коллизий {collisions} ===")


if __name__ == "__main__":
    main()
