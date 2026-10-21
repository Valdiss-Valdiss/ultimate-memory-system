"""Догенерация LLM-резюме: заметки с '## Транскрипт' и 'Резюме: _(нет)_'.
Запуск: python regen_summaries.py [--limit N]
Идемпотентен: регенерированные пропускает при повторном запуске."""
import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import yt_enrich as ye

VAULT = Path(r"C:\Users\<user>\Documents\Obsidian Vault")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    todo = []
    for p in VAULT.rglob("*.md"):
        try:
            body = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "## Транскрипт" in body and re.search(r"- \*\*Резюме:\*\* _\(нет\)_", body):
            todo.append(p)
    todo.sort(key=lambda p: -p.stat().st_size)
    if args.limit:
        todo = todo[:args.limit]
    print(f"к догенерации: {len(todo)}", flush=True)
    ok = fail = 0
    for i, p in enumerate(todo, 1):
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
            transcript = content.split("## Транскрипт", 1)[1].strip()
            summary = ye.llm_summary(transcript, "")
            if not summary:
                fail += 1
                print(f"  [{i}/{len(todo)}] ПУСТО: {p.name[:60]}", flush=True)
                continue
            content = content.replace("- **Резюме:** _(нет)_",
                                      "- **Резюме:** " + summary, 1)
            p.write_text(content, encoding="utf-8")
            ok += 1
            print(f"  [{i}/{len(todo)}] ok ({len(summary)} зн.): {p.name[:60]}",
                  flush=True)
        except Exception as e:
            fail += 1
            print(f"  [{i}/{len(todo)}] ОШИБКА {type(e).__name__}: {p.name[:60]}",
                  flush=True)
        time.sleep(2)
    print(f"=== Итог: сгенерировано {ok}, ошибок {fail} ===", flush=True)


if __name__ == "__main__":
    main()
