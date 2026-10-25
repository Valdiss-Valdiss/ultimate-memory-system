#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ретранслятор: health.md (канарь ПК) → сообщение в чат бота Hermes.

2026-10-25 (решение пользователя): канарь оповещает о проблемах Memory System
в чат с ботом Hermes. Токен и chat_id читаются ВНУТРИ hermes-контейнера
(не копируются наружу, не печатаются). Отправка через Telegram Bot API.

Дедуп (state-файл):
  - статус != "в норме" и предыдущий был "в норме" → отправить список проблем;
  - проблема продолжается ≥6 ч с последнего напоминания → напомнить;
  - статус вернулся в "в норме" после проблемы → отправить "восстановлено";
  - health.md не обновлялся ≥3 ч → однократное "канарь молчит";
  - прочие прогоны → тишина.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HEALTH = Path("/srv/memory-system/vault/memory-ai/Inbox/AI/Status/health.md")
STATE = Path("/opt/memory-stack/prod/state/health_relay.json")
LOG = Path("/opt/memory-stack/tg-bot/logs/health_relay.log")
LOCK = Path("/opt/memory-stack/prod/state/health_relay.lock")
REMINDER_H = 6.0
SILENCE_H = 3.0
HEARTBEAT_H = 24.0


def log(msg):
    # 2026-10-25: только файл (cron-редирект печатал дубль каждой строки);
    # traceback-и всё равно попадают в лог через stderr перенаправления cron
    line = datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + str(msg)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def read_creds():
    """(token, chat_id) — изнутри hermes-контейнера; значения не печатаются."""
    tok = subprocess.run(
        ["docker", "exec", "hermes", "sh", "-c",
         "grep '^TELEGRAM_BOT_TOKEN=' /opt/data/.env | cut -d= -f2- | tr -d '\"\\r'"],
        capture_output=True, text=True, timeout=30).stdout or ""
    chat = subprocess.run(
        ["docker", "exec", "hermes", "sh", "-c",
         "grep '^TELEGRAM_ALLOWED_USERS=' /opt/data/.env | cut -d= -f2- | cut -d, -f1 | tr -d '\"\\r'"],
        capture_output=True, text=True, timeout=30).stdout or ""
    return tok.strip(), chat.strip()


def send(token, chat, text):
    url = "https://api.telegram.org/bot" + token + "/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": chat, "text": text[:3500]}).encode()
    req = urllib.request.Request(url, data=data)
    urllib.request.urlopen(req, timeout=30).read()


def parse_health(text):
    status = "в норме"
    m = re.search(r"^Статус:\s*(.+)$", text, re.M)
    if m:
        status = m.group(1).strip()
    plist = []
    sec = re.search(r"## Требует внимания\s*\n(.*?)(?:\n## |\Z)", text, re.S)
    if sec:
        for l in sec.group(1).splitlines():
            if l.strip().startswith("-"):
                plist.append(l.strip().lstrip("-* ").strip())
    return status, plist


def main():
    # 2026-10-25: замок от гонки (два cron-прогона в одну секунду → два сообщения)
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
    except FileExistsError:
        # чужой замок старше 5 мин = забытый (умерший пргон) — снести и работать
        try:
            if time.time() - LOCK.stat().st_mtime > 300:
                LOCK.unlink()
            else:
                return
        except Exception:
            return
    args = sys.argv[1:]
    try:
        if "--test" in args:
            tok, chat = read_creds()
            if not tok or not chat:
                log("[relay] тест: креды не получены (token=%s chat=%s)" % (bool(tok), bool(chat)))
                return
            send(tok, chat, "🧪 Тест оповещения Memory System: канал связи с ботом Hermes работает.")
            log("[relay] тест отправлен")
            return
        _main_inner()
    finally:
        try:
            LOCK.unlink()
        except Exception:
            pass


def _main_inner():
    state = {}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    if not HEALTH.exists():
        log("[relay] health.md нет — ничего не делаю (канарь ещё ни разу не писал)")
        return
    text = HEALTH.read_text(encoding="utf-8", errors="replace")
    status, plist = parse_health(text)
    age_h = (time.time() - HEALTH.stat().st_mtime) / 3600.0
    is_problem = status.lower() != "в норме"
    last = state.get("last_status", "в норме")
    last_rem = state.get("last_reminder", 0)
    warned_stale = state.get("warned_stale", False)

    try:
        if is_problem:
            body = "; ".join(plist[:5]) or status
            if last == "в норме":
                send(*read_creds(), "⚠️ Memory System: проблема\n" + body)
                log("[relay] отправлено: проблема (" + body[:100] + ")")
                state.update(last_status=status, last_reminder=time.time(), warned_stale=False)
            elif time.time() - last_rem >= REMINDER_H * 3600:
                send(*read_creds(), "🔁 Напоминание (проблема продолжается):\n" + body)
                log("[relay] отправлено: напоминание (" + body[:100] + ")")
                state.update(last_status=status, last_reminder=time.time(), warned_stale=False)
            else:
                log("[relay] проблема известна, тишина до напоминания")
        else:
            if age_h > SILENCE_H and not warned_stale:
                send(*read_creds(), "⚠️ Memory System: канарь не обновляет health.md "
                     "уже %.1f ч (ПК спал / Obsidian закрыт / задача не сработала)." % age_h)
                log("[relay] отправлено: канарь молчит (%.1f ч)" % age_h)
                state.update(warned_stale=True, last_status="в норме")
            elif last != "в норме":
                send(*read_creds(), "✅ Memory System: восстановлено.")
                log("[relay] отправлено: восстановлено")
                state.update(last_status="в норме", warned_stale=False, last_heartbeat=time.time())
            else:
                # 2026-10-25 (запрос юзера): ежедневный контрольный сигнал «всё штатно»,
                # чтобы тишина не была двусмысленной
                if time.time() - state.get("last_heartbeat", 0) >= HEARTBEAT_H * 3600:
                    send(*read_creds(), "✅ Memory System: штатная работа (ежедневный контрольный сигнал).")
                    log("[relay] отправлено: ежедневный сигнал «всё штатно»")
                    state.update(last_heartbeat=time.time())
                else:
                    log("[relay] в норме, тишина")
    except Exception as e:
        log("[relay] сбой отправки: %s: %s" % (type(e).__name__, str(e)[:100]))

    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE)
    except Exception:
        pass


if __name__ == "__main__":
    main()
