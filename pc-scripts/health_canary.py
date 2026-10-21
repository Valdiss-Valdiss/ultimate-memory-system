# -*- coding: utf-8 -*-
"""Канари здоровья конвейера (ПК).

Раз в час проверяет всю цепочку и пишет компактный отчёт в заметку
Inbox/AI/Status/health.md (её видно в Obsidian и находит поиск) + строку в catchup.log.

Проверки:
  1. Публичный IP (с фолбэк-сервисами) — нужен серверному whitelist-синку.
  2. SSH до VPS (paramiko) — если недоступен, вероятна смена IP/VPN.
  3. LLM-ключ (1-токенная проба, без побочных эффектов для cooldown-стейта).
  4. CouchDB (HTTPS) — 401/200 = сервер жив.
  5. Очереди бэкфилла (фото/док) — должны быть 0.
  6. Живость бэкфилла (mtime логов + процесс).
  7. Свежесть Telegram-догона (последний hourly catchup в catchup.log).
  8. Duplicati: время и результат последнего бэкапа (его sqlite, только чтение).

Запись в vault — только при открытом Obsidian (vault_guard), иначе отчёт идёт
только в catchup.log (чтобы не писать в vault при закрытом Obsidian).
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
VAULT = Path(os.environ.get("VAULT_DIR", r"C:\Users\<user>\Documents\Obsidian Vault"))
NOTE_REL = Path("Inbox") / "AI" / "Status" / "health.md"
CATCHUP_LOG = SCRIPT_DIR / "catchup.log"
IP_CACHE = SCRIPT_DIR / "canary_last_ip.txt"
LIVESYNC_HOST = os.environ.get("LIVESYNC_HOST", "https://couchdb.YOUR-DOMAIN")

import vault_guard  # noqa: E402

IP_SERVICES = [
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
    "https://ident.me",
]


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)
    print(line, flush=True)
    try:
        with open(CATCHUP_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def public_ip():
    for url in IP_SERVICES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "memory-canary/1.0"})
            ip = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "replace").strip()
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
                return ip
        except Exception:
            continue
    try:
        return IP_CACHE.read_text(encoding="utf-8").strip() or "неизвестен (кэш пуст)"
    except Exception:
        return "неизвестен"


def check_ssh():
    try:
        import vps_ssh
        rc, out, err = vps_ssh.exec_("echo canary-ok", timeout=25)
        ok = rc == 0 and "canary-ok" in (out or "")
        vps_ssh.close()
        return ("ok" if ok else f"rc={rc}")
    except Exception as e:
        return f"недоступен ({type(e).__name__})"


def check_llm():
    try:
        import yt_enrich
        cands = yt_enrich.llm_candidates()
        if not cands:
            return "нет ключей"
        body = json.dumps({"model": yt_enrich.LLM_MODEL, "max_tokens": 2,
                           "messages": [{"role": "user", "content": "ping"}]}).encode()
        for label, key in cands:
            d, err, code = yt_enrich._chat_once(body, key, 45)
            if d is not None:
                return f"жив ({label})"
            if code == 429:
                return f"лимит окна ({label}:429)"
            if code in (401, 403):
                return f"КЛЮЧ ОТКАЗАН ({label}:{code})"
            return f"ошибка ({label}:{code} {str(err)[:60]})"
        return "нет ключей"
    except Exception as e:
        return f"ошибка проверки ({type(e).__name__})"


def check_couch():
    try:
        req = urllib.request.Request(LIVESYNC_HOST.rstrip("/") + "/", method="HEAD",
                                     headers={"User-Agent": "memory-canary/1.0"})
        urllib.request.urlopen(req, timeout=15)
        return "ok (200)"
    except urllib.error.HTTPError as e:
        return f"ok ({e.code})" if e.code in (200, 401, 403) else f"HTTP {e.code}"
    except Exception as e:
        return f"недоступен ({type(e).__name__})"


def check_queues():
    try:
        import photos_backfill as pbf
        import docs_backfill as dbf
        return f"фото={len(pbf.scan_targets())} док={len(dbf.scan_targets())}"
    except Exception as e:
        return f"ошибка ({type(e).__name__})"


def _mtime_age(path):
    try:
        return f"{int((time.time() - path.stat().st_mtime) / 60)} мин"
    except Exception:
        return "нет файла"


def check_backfill_live():
    parts = []
    for name in ("attachments_backfill.log", "photos_backfill.log"):
        parts.append(f"{name.replace('.log', '')}={_mtime_age(SCRIPT_DIR / name)}")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Select-Object -ExpandProperty CommandLine"],
            capture_output=True, text=True, encoding="cp1251", errors="replace", timeout=40).stdout or ""
        parts.append("процесс=да" if "attachments_backfill" in out else "процесс=нет")
    except Exception:
        parts.append("процесс=?")
    return ", ".join(parts)


def check_telegram_fresh():
    try:
        data = CATCHUP_LOG.read_text(encoding="utf-8", errors="replace")
        hits = re.findall(
            r"\[(\d{2}\.\d{2}\.\d{4}\s+\d{1,2}:\d{2}:\d{2},\d+)\] === hourly catchup ===", data)
        if not hits:
            return "нет прогона в логе"
        last = hits[-1]
        return f"последний прогон {last}"
    except Exception as e:
        return f"ошибка проверки ({type(e).__name__})"


def check_duplicati():
    """Время последнего успешного бэкапа: свежий Type='Result' в per-job БД."""
    try:
        import glob
        best = 0
        best_job = ""
        for db in glob.glob(os.path.expandvars(r"%LOCALAPPDATA%\Duplicati\*.sqlite")):
            if "Duplicati-server" in db:
                continue
            try:
                con = sqlite3.connect("file:" + db + "?mode=ro&immutable=1", uri=True)
                cur = con.cursor()
                cur.execute("SELECT Timestamp FROM LogData WHERE Type='Result' "
                            "ORDER BY Timestamp DESC LIMIT 1")
                row = cur.fetchone()
                con.close()
                if row and row[0] and int(row[0]) > best:
                    best = int(row[0])
                    best_job = os.path.basename(db).replace(".sqlite", "")
            except Exception:
                continue
        if not best:
            return "бэкапов не найдено"
        when = datetime.fromtimestamp(best).strftime("%Y-%m-%d %H:%M")
        age_h = (time.time() - best) / 3600.0
        return f"последний бэкап {when} ({age_h:.1f} ч назад, job {best_job})"
    except Exception as e:
        return f"ошибка проверки ({type(e).__name__})"


def main():
    ip = public_ip()
    try:
        IP_CACHE.write_text(ip, encoding="utf-8")
    except Exception:
        pass
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = [
        ("Время", now),
        ("Публичный IP", ip),
        ("SSH до VPS", check_ssh()),
        ("LLM-ключ", check_llm()),
        ("CouchDB (HTTPS)", check_couch()),
        ("Очереди бэкфилла", check_queues()),
        ("Бэкфилл (свежесть логов)", check_backfill_live()),
        ("Telegram-догон", check_telegram_fresh()),
        ("Duplicati", check_duplicati()),
    ]
    problems = [f"{k}: {v}" for k, v in rows[1:]
                if any(bad in str(v).lower() for bad in (
                    "недоступен", "отказ", "ошибка проверки", "нет ключей",
                    "неизвестен", "процесс=нет", "нет прогона", "http 5"))]
    status = "ПРОБЛЕМЫ" if problems else "в норме"
    log(f"[canary] {status} | ip={ip} | ssh={rows[2][1]} | llm={rows[3][1]} | queues={rows[5][1]}")

    body = ["---", "", f"Обновлено: {now}", f"Статус: {status}", ""]
    body += [f"- **{k}:** {v}" for k, v in rows[1:]]
    if problems:
        body += ["", "## Требует внимания", ""] + [f"- {p}" for p in problems]
    body += [""]
    text = "\n".join(body)

    if vault_guard.obsidian_running():
        try:
            note = VAULT / NOTE_REL
            note.parent.mkdir(parents=True, exist_ok=True)
            fm = ("---\ntype: отчёт\ncreated: 2026-10-21\nupdated: "
                  + datetime.now().strftime("%Y-%m-%d")
                  + "\nsource: opencode\nstatus: draft\ntags: [system, health]\n---\n\n")
            note.write_text(fm + text, encoding="utf-8")
            log(f"[canary] отчёт записан: {NOTE_REL}")
        except Exception as e:
            log(f"[canary] не смог записать заметку: {type(e).__name__}: {e}")
    else:
        log("[canary] Obsidian закрыт — отчёт только в catchup.log (vault не трогаем)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"[canary] сбой: {type(e).__name__}: {e}")
        raise
