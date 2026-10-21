# -*- coding: utf-8 -*-
"""Общий guard для скриптов, которые пишут в vault снаружи Obsidian.

Причина: инцидент 15–16.09 — внешние записи в vault при закрытом Obsidian
(патч бэкфилла) привели к волне удалений, потому что плагин LiveSync затёр
локальные изменения при pull'е с сервера.

Правило: любой скрипт, который создаёт/переносит/правит файлы в vault, обязан
ДО любых действий вызвать require_obsidian() и выйти, если Obsidian не запущен.
Курсор Telegram при этом не сдвигается — работа подхватится в следующем прогоне.

Тест «закрытого» состояния: задать переменную окружения VAULT_GUARD_PROC=nonexistent.exe
— guard считает Obsidian закрытым (удобно для проверки без закрытия реального Obsidian).
"""
import os
import subprocess

PROC = os.environ.get("VAULT_GUARD_PROC", "Obsidian.exe")


def obsidian_running(proc: str = None) -> bool:
    proc = proc or PROC
    try:
        # tasklist печатает в кодировке консоли (не UTF-8) — декодируем мягко,
        # иначе UnicodeDecodeError даёт ложное «Obsidian не запущен».
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {proc}"],
            capture_output=True, text=True,
            encoding="cp1251", errors="replace", timeout=30).stdout or ""
    except Exception:
        return False
    return proc.lower() in out.lower()


def require_obsidian(log, what: str = "") -> bool:
    """True — можно писать в vault. False — писать нельзя (вызывающий обязан выйти)."""
    if obsidian_running():
        return True
    tail = f" ({what})" if what else ""
    log(f"[!] Obsidian не запущен — внешние записи в vault запрещены{tail}. "
        f"Пропуск: ничего не изменено, работа подхватится в следующем прогоне.")
    return False
