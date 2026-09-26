# vps/bot — сборка серверного образа конвейера

Исходники кода конвейера — **общие с `pc-scripts/`** (одинаковые .py на ПК и в контейнере).
Этот каталог содержит только Dockerfile + requirements + сборочный скрипт, чтобы
не дублировать код.

## Сборка

```bash
# 1. скопировать исходники .py из pc-scripts в этот каталог
bash build.sh
# 2. собрать образ
docker build -t memory-tg-tools:latest .
```

build.sh копирует `../pc-scripts/*.py` сюда (плюс создаёт пустой folders.yaml,
если его нет — реальный folders.yaml монтируется/копируется отдельно, не коммитится).

## Что внутри контейнера

- `tg_bot.py` — watcher каналов (Bot API), складывает посты в staging;
- `tg_media.py` — загрузчик медиа-вложений;
- `enrich_driver.py` — обогащение: LLM-резюме, транскрипты, описания;
- `memory_indexer.py` — семантический индекс зеркала (чанки → TEI → Qdrant);
- `*_backfill.py` / `*_lib.py` / `tg_common.py` — общие библиотеки конвейера.

## Зависимости (requirements.txt)

telethon, requests, pypdfium2, pillow (+ ffmpeg в образе; imageio-ffmpeg).

## Переменные

Берутся из `.env` стека (env_file в compose): API_ID/API_HASH/BOT_TOKEN/LLM_KEY и т.д.
