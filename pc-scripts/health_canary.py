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
import csv
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
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
                return f"жив ({label}, {yt_enrich.LLM_MODEL})"
            txt = str(err or "")
            # Провайдер убрал модель из каталога: конвейер молча перестаёт делать
            # резюме/OCR. Это ломает всё обогащение — отмечаем явно.
            if "Unsupported model" in txt or "Model is unavailable" in txt \
                    or "does not support" in txt:
                return (f"МОДЕЛЬ НЕДОСТУПНА ({yt_enrich.LLM_MODEL}) — "
                        f"смени LLM_MODEL (см. runbook-llm-keys.md)")
            if code == 429:
                return f"лимит окна ({label}:429)"
            if code in (401, 403):
                return f"КЛЮЧ ОТКАЗАН ({label}:{code})"
            return f"ошибка ({label}:{code} {txt[:60]})"
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
    """(строка для отчёта, (фото, док)) — счётчики нужны бэкфилл-проверке."""
    try:
        import photos_backfill as pbf
        import docs_backfill as dbf
        p = len(pbf.scan_targets())
        d = len(dbf.scan_targets())
        return f"фото={p} док={d}", (p, d)
    except Exception as e:
        return f"ошибка ({type(e).__name__})", (0, 0)


def _mtime_age(path):
    try:
        return f"{int((time.time() - path.stat().st_mtime) / 60)} мин"
    except Exception:
        return "нет файла"


def check_backfill_live(pending=0):
    """2026-10-25: 'процесса нет' — проблема ТОЛЬКО при непустой очереди И
    если часовая цепочка давно не подхватывала (>70 мин). Штатный простой
    (очередь пуста) и свежая цепочка (подхватит в течение часа) — не проблемы:
    раньше ложные тревоги били на каждом простое/каждой новой партии."""
    parts = []
    for name in ("attachments_backfill.log", "photos_backfill.log"):
        parts.append(f"{name.replace('.log', '')}={_mtime_age(SCRIPT_DIR / name)}")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Select-Object -ExpandProperty CommandLine"],
            capture_output=True, text=True, encoding="cp1251", errors="replace", timeout=40).stdout or ""
        running = "attachments_backfill" in out
    except Exception:
        parts.append("процесс=?")
        return ", ".join(parts)
    if running:
        parts.append("процесс=да")
    elif pending > 0:
        age = _last_catchup_age_min()
        if age is not None and age <= 70:
            parts.append(f"очередь {pending} — цепочка недавняя, подхватит сама (штатно)")
        else:
            parts.append(f"ПРОЦЕССА НЕТ при очереди {pending} (цепочка не подхватывает >1 ч!)")
    else:
        parts.append("простой (очередь пуста)")
    return ", ".join(parts)


def _last_catchup_age_min():
    """Возраст последнего часового прогона (мин) или None."""
    try:
        data = CATCHUP_LOG.read_text(encoding="utf-8", errors="replace")
        hits = re.findall(
            r"\[(\d{2}\.\d{2}\.\d{4}\s+\d{1,2}:\d{2}:\d{2},\d+)\] === hourly catchup ===", data)
        if not hits:
            return None
        dt = datetime.strptime(hits[-1].split(",")[0], "%d.%m.%Y %H:%M:%S")
        return (datetime.now() - dt).total_seconds() / 60
    except Exception:
        return None


def check_telegram_fresh():
    """Свежесть догона + возраст зомби-процесса (2026-10-25, инцидент 22.10:
    зависший прогон прожил 2 дня и молча заблокировал все следующие)."""
    age = _last_catchup_age_min()
    if age is None:
        return "ПРОСРОЧЕН: нет прогона в логе"
    if age > 90:
        return (f"ПРОСРОЧЕН: последний прогон "
                f"{datetime.now() - timedelta(minutes=age):%d.%m.%Y %H:%M:%S} "
                f"({age / 60:.1f} ч назад)")
    return (f"последний прогон "
            f"{datetime.now() - timedelta(minutes=age):%d.%m.%Y %H:%M:%S} "
            f"({int(age)} мин назад)")


def _cim_age_min(s):
    """Возраст в минутах из WMI-даты (yyyyMMddHHmmss.ffffff±060, UTC)."""
    try:
        dt = datetime.strptime(s[:14], "%Y%m%d%H%M%S")
        m = re.search(r"([+-])(\d{3})$", s)
        if m:
            off = int(m.group(2))
            dt = dt - timedelta(minutes=off) if m.group(1) == "+" else dt + timedelta(minutes=off)
        return (datetime.utcnow() - dt).total_seconds() / 60
    except Exception:
        return None


def remediate_catchup():
    """Зомби-страховка №2 (кроме StopExisting задачи): pc_catchup.py, живущий
    >30 мин, убивается — он к тому времени обязан завершиться (свой потолок 25 мин).
    Возвращает описание для отчёта."""
    try:
        ps = ('Get-CimInstance Win32_Process -Filter "Name=\'python.exe\'" | '
              'Where-Object { $_.CommandLine -match \'pc_catchup\\.py\' } | '
              'Select-Object ProcessId, CreationDate | ConvertTo-Csv -NoTypeInformation')
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, encoding="cp1251", errors="replace",
            timeout=40).stdout or ""
        rows = list(csv.DictReader(out.splitlines()))
    except Exception as e:
        return f"ошибка проверки ({type(e).__name__})"
    killed, alive = [], []
    for r in rows:
        try:
            pid = int(r.get("ProcessId") or 0)
        except Exception:
            continue
        if not pid or pid == os.getpid():
            continue
        age = _cim_age_min(r.get("CreationDate") or "")
        if age is None or age > 30:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=30)
            age_txt = f"{age:.0f} мин" if age is not None else "?"
            killed.append(f"PID {pid} (возраст {age_txt})")
        else:
            alive.append(f"PID {pid}, {age:.0f} мин")
    if killed:
        return f"УБИТ ЗОМБИ: {', '.join(killed)} (держал сессию/лог)"
    if alive:
        return f"прогон жив и молод: {', '.join(alive)}"
    return "не требовалось"


def check_containers():
    """Живость серверных контейнеров конвейера (2026-10-25)."""
    need = ["memory_tg_bot", "memory_tgapi", "memory_tg_media"]
    try:
        import vps_ssh
        rc, out, err = vps_ssh.exec_(
            "docker ps --format '{{.Names}}'", timeout=30)
        vps_ssh.close()
        if rc != 0:
            return f"ПРОБЛЕМА КОНТЕЙНЕРОВ: docker ps rc={rc}"
        names = set(n.strip() for n in (out or "").splitlines() if n.strip())
        couch = [n for n in names if "couchdb" in n]
        missing = [n for n in need if n not in names]
        if not couch:
            missing.append("(couchdb)")
        if missing:
            return f"ПРОБЛЕМА КОНТЕЙНЕРОВ: нет {', '.join(missing)} (на месте {len(need) - len(missing) + len(couch)}/4)"
        return f"{len(need) + len(couch)}/4 на месте"
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
    heal = remediate_catchup()
    ip = public_ip()
    try:
        IP_CACHE.write_text(ip, encoding="utf-8")
    except Exception:
        pass
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    q_str, q_counts = check_queues()
    rows = [
        ("Время", now),
        ("Публичный IP", ip),
        ("SSH до VPS", check_ssh()),
        ("LLM-ключ", check_llm()),
        ("CouchDB (HTTPS)", check_couch()),
        ("Контейнеры VPS", check_containers()),
        ("Очереди бэкфилла", q_str),
        ("Бэкфилл (свежесть логов)", check_backfill_live(q_counts[0] + q_counts[1])),
        ("Telegram-догон", check_telegram_fresh()),
        ("Зомби-лечение", heal),
        ("Duplicati", check_duplicati()),
    ]
    problems = [f"{k}: {v}" for k, v in rows[1:]
                if any(bad in str(v).lower() for bad in (
                    "недоступен", "отказ", "ошибка проверки", "нет ключей",
                    "неизвестен", "процесса нет", "просрочен",
                    "проблема контейнеров", "нет прогона", "http 5",
                    "убит зомби"))]
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
