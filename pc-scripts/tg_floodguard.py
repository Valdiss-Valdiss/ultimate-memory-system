# -*- coding: utf-8 -*-
"""Общий страж flood-лимита Telegram (429/InvalidBuffer) для всех скриптов, бьющих в юзер-сессию.

Механика: файл-маркер tg_cooldown_until.txt с unix-временем окончания охлаждения.
Любой скрипт при старте зовёт guard_or_exit() — если охлаждение активно, завершается сразу.
При пойманном FloodWait/429 скрипт зовёт set_cooldown() и выходит, не выжигая лимит ретраями.
"""
import time
from pathlib import Path

CODE = Path(__file__).resolve().parent
COOLDOWN_FILE = CODE / "tg_cooldown_until.txt"

DEFAULT_MINUTES = 60


def cooldown_remaining():
    try:
        return float(COOLDOWN_FILE.read_text(encoding="utf-8").strip()) - time.time()
    except Exception:
        return 0.0


def set_cooldown(minutes=DEFAULT_MINUTES, note=""):
    until = time.time() + minutes * 60
    COOLDOWN_FILE.write_text(str(until), encoding="utf-8")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(until))
    return f"[429] flood: охлаждение сессии {int(minutes)} мин (до {when}) {note}".strip()


def is_flood_error(e):
    name = type(e).__name__
    s = str(e)
    return (
        name in ("FloodWaitError", "FloodError", "RPCError")
        and "Flood" in name
    ) or "Flood" in name or "429" in s or "InvalidBuffer" in s or "flood" in s.lower()


class FloodHit(Exception):
    pass


def guard_or_exit(log):
    """True — идём в охлаждение (скрипт должен завершиться, Telegram не трогать)."""
    rem = cooldown_remaining()
    if rem > 0:
        log(f"[429] охлаждение Telegram-сессии ещё {int(rem // 60)} мин — пропуск запуска")
        return True
    return False
