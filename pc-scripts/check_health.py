# -*- coding: utf-8 -*-
"""Проверка здоровья конвейера (health-check). Только чтение, без сети.

Показывает: очереди целей, финализацию контрольных заметок, hourly-цепочку,
лог-хвосты конвертаций, живые процессы, cooldown-лейблы ключей.
Запуск: python check_health.py
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

VAULT = Path(os.environ.get("VAULT_DIR", r"C:\Users\<user>\Documents\Obsidian Vault"))

CONTROL_NOTES = [
    "Growth/2025-02-12-report.md",
    "Growth/2025-02-12-report-alt.md",
    "Growth/2025-02-14-report.md",
    "Growth/2025-02-14-report-alt.md",
]


def section(title):
    print("", flush=True)
    print("=== " + title + " ===", flush=True)


def lbs(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


section("1. Очереди (что осталось обработать)")
try:
    import docs_backfill as dbf
    import photos_backfill as pbf
    d = dbf.scan_targets()
    p = pbf.scan_targets()
    print(f"документы: {len(d)} целей; фото: {len(p)} целей")
    for t in d[:5]:
        print("  docs цель:", t[2].name, f"(msg {t[1]})")
except Exception as e:
    print("скан не удался:", type(e).__name__, str(e)[:100])

section("2. Контрольные 4 заметки (восстановленные вчера)")
for rel in CONTROL_NOTES:
    p = VAULT / rel
    if not p.exists():
        print(f"  {rel}: ФАЙЛ ПРОПАЛ!")
        continue
    t = p.read_text(encoding="utf-8", errors="replace")
    sec = len(re.findall(r"(?m)^## \S+ Документ: ", t))
    has = "has_doc_text: true" in t
    mk = len(re.findall(r"не обработан", t))
    status = "OK" if (sec >= 5 and has and mk == 0) else "ПРОВЕРИТЬ"
    print(f"  {rel}: секций={sec}, текст есть={has}, маркеров={mk}  [{status}]")

section("3. Часовая цепочка (последние прогоны)")
hdr = []
logp = SCRIPT_DIR / "catchup.log"
try:
    with logp.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if "hourly catchup" in line:
                hdr.append(line.strip()[:60])
except Exception as e:
    print("catchup.log не читается:", str(e)[:80])
print("прогонов всего:", len(hdr))
for h in hdr[-5:]:
    print("  ", h)

section("4. Свежие конвертации (последние результаты)")
for name in ("docs_backfill.log", "photos_backfill.log"):
    fp = SCRIPT_DIR / name
    lines = fp.read_text(encoding="utf-8", errors="replace").splitlines() if fp.exists() else []
    plus = [l for l in lines if "[+]" in l][-3:]
    err24 = time.time() - 24 * 3600
    errs_today = 0
    for l in lines:
        m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", l)
        if l.startswith("    ") or len(l) < 18:
            continue
        try:
            ts = time.mktime(time.strptime(l[:19], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        if ts >= err24 and "[err]" in l:
            errs_today += 1
    print(f"{name}: ошибок за 24ч: {errs_today}")
    for l in plus:
        print("  ", l[:110])

section("5. Процессы и Obsidian")
out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq python.exe"], capture_output=True, text=True, encoding="cp1251", errors="replace").stdout
pyc = out.lower().count("python.exe")
print("python.exe процессов:", pyc)
out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Obsidian.exe"], capture_output=True, text=True, encoding="cp1251", errors="replace").stdout
print("Obsidian открыт:", "obsidian.exe" in out.lower())

section("6. Cooldown LLM-ключей (ПК; лейблы, без секретов)")
stf = SCRIPT_DIR / "llm_key_cooldown.json"
try:
    import json
    reg = json.loads(stf.read_text(encoding="utf-8"))
    if not reg:
        print("  пусто (все ключи свободны)")
    now = time.time()
    for label, st in sorted(reg.items()):
        rem_h = (float(st.get("until", 0)) - now) / 3600
        print(f"  {label}: в cooldown ещё {rem_h:.0f} ч")
except FileNotFoundError:
    print("  файла нет (ключи свободны)")
except Exception as e:
    print("  state:", type(e).__name__, str(e)[:80])

print("", flush=True)
print("HEALTH-CHECK DONE", flush=True)
