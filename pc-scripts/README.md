# pc-scripts — клиентский конвейер (ПК/ноутбук)

## Что это

Скрипты, которые превращают посты Telegram в полноценные заметки Obsidian:
догон пропущенных постов, скачивание вложений, OCR/описания фото (LLM),
конвертация документов (docling), транскрипты YouTube, раскладка по папкам,
переименование авто-имён. Плюс стражи (flood-guard, guard-рестарт) и часовая цепочка.

## Предусловия

1. Obsidian установлен, vault существует (путь будет в переменной окружения).
2. Python 3.12+ с пакетами: `telethon`, `python-dotenv` (остальное — опционально).
3. Telegram API_ID/API_HASH (my.telegram.org) в `.env` рядом со скриптами.
4. Серверная часть уже развёрнута (vps/) — скрипты общаются с ней по SSH.
5. Ключ SSH для сервера (id_ed25519) в `~/.ssh/`.

## Переменные окружения (агент задаёт на машине)

| Переменная | Пример | Кто использует |
|---|---|---|
| `MEMORY_VAULT_DIR` | `C:\Users\<user>\Documents\Obsidian Vault` | почти все скрипты |
| `MEMORY_VPS_TARGET` | `user@example.com` | pc_catchup, бэкфиллы (state-обмен) |
| `MEMORY_SSH_KEY` | `C:\Users\<user>\.ssh\id_ed25519` | SSH-вызовы |

Альтернатива: отредактировать константы в скриптах (помечены `CHANGE-ME`).

## Файлы данных (создаёт агент, в папке скриптов)

- `.env` — API_ID, API_HASH, ключи LLM/транскриптов (по образцу ниже), chmod/600;
- `session/` — файл Telegram user-сессии (создаёт `tg_login.py`, хранить бережно);
- `channels.json` — от серверной части (список каналов: id → title);
- `folders.yaml` — маппинг каналов в папки vault (агент создаёт с юзером).

## Установка (агент)

1. Скопировать `pc-scripts/` в рабочую папку (например, `C:\projects\memory\`).
2. `pip install telethon python-dotenv`.
3. Заполнить `.env` (секреты!), запустить `python tg_login.py` — создать user-сессию.
4. Создать запланированную задачу «hourly»: запуск `catchup.bat` каждый час
   (Планировщик задач Windows → запускается при включённом ПК).
5. Проверить `validate/validate_pc.py` — до зелёного.

## catchup.bat — часовая цепочка (строго последовательная!)

```bat
python pc_catchup.py        >> catchup.log 2>&1
python triage_folders.py    >> catchup.log 2>&1
powershell -File backfill_autorestart.ps1 >> catchup.log 2>&1
python note_renamer.py --execute >> catchup.log 2>&1
```

Порядок важен: catchup → разложить → поднять бэкфиллы → переименовать.
`backfill_autorestart.ps1` поднимает бэкфиллы, ЕСЛИ они не запущены (single client).

## Новые модули (автономность)

| Файл | Зачем |
|---|---|
| `vps_ssh.py` | Одно постоянное SSH-соединение (paramiko) на весь прогон — вместо 5–6 новых на каждый файл. Снимает rate-limit и «banner storm» |
| `vault_guard.py` | Запрет писать в vault при закрытом Obsidian. Подключён в `pc_catchup` (до выборок, курсор не двигается) и `triage_folders` |
| `health_canary.py` | Канари: раз в час проверяет всю цепочку, пишет отчёт в `Inbox/AI/Status/health.md` (в vault). Запуск вручную: `python health_canary.py` |
| `check_health.py` | Быстрая сводка состояния ПК (очереди, процессы, cooldown) |
| `media_re_summary.py` | Догенерация пустых медиа-резюме (по транскрипту, а при его отсутствии — по тексту поста) |

**Задачи Windows**: «Memory Catchup Hourly» — часовая цепочка; «Memory Health
Canary» — канари. Обе должны быть включены; у часовой задачи важно
`StartWhenAvailable` (добирает пропущенные прогоны после сна).


## Правила эксплуатации (для юзера)

- **Obsidian держать открытым** — бэкфиллы пишут в vault только при открытом Obsidian.
- **Ноутбук в розетку** + сон отключён (иначе ночные конвейеры умирают —
  см. runbook-windows-quirks.md).
- Один Telegram-клиент за раз — встроено в скрипты (yield/floodguard).

## Ключевые скрипты (что делает каждый)

| Скрипт | Роль |
|---|---|
| `pc_catchup.py` | Часовой догон: state с VPS → новые посты → готовые заметки → state назад |
| `attachments_backfill.py` | ОДИН процесс на docs+photos (одна сессия), идемпотентно |
| `docs_backfill.py` / `photos_backfill.py` | Фазы: документы (docling+LLM) / фото (mimo) |
| `triage_folders.py` | Раскладка из входной папки в целевые по frontmatter |
| `note_renamer.py` | Авто-имена → осмысленные (по OCR/резюме), коллизии через msg_id |
| `tg_floodguard.py` | Страж 429: маркер охлаждения, мгновенный выход |
| `memory_indexer.py` | Локальный вариант индексатора (если зеркало на ПК) |
| `tg_login.py` | Создание/проверка user-сессии |
