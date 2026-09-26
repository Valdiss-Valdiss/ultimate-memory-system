"""Логин Telethon-сессии на ПК (один раз, интерактивно).

Запуск в СВОЁМ терминале:  python tg_login.py
Скрипт сам запросит: телефон (если не передан --phone), код из Telegram,
пароль 2FA (если включён). Всё вводится только здесь — код и пароль нигде
не сохраняются и не печатаются. После успеха сессия хранится в session/.
"""
import argparse
import asyncio
from pathlib import Path

from telethon import TelegramClient

from tg_common import SCRIPT_DIR, load_env

SESSION_DIR = SCRIPT_DIR / "session"


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phone", default=None, help="+7XXXXXXXXXX (иначе спросит)")
    args = parser.parse_args()

    env = load_env()
    SESSION_DIR.mkdir(exist_ok=True)
    client = TelegramClient(str(SESSION_DIR / env.get("SESSION_NAME", "vault_import")),
                            int(env["API_ID"]), env["API_HASH"])
    phone = args.phone
    await client.start(phone=phone)
    me = await client.get_me()
    print(f"[OK] Вход выполнен: {me.first_name} (id {me.id}). Сессия сохранена в session/.")
    print("     Повторный запуск не нужен. Отзыв сессии после миграции: Telegram →")
    print("     Настройки → Устройства → завершить сеанс vault_import (+ удалить session/).")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
