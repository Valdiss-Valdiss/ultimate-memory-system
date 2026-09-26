# Runbook — VPS setup (развёртывание и восстановление серверной части)

> **Уже есть сервер на selfhost-ai?** Смотри «Сценарий B» в vps/README.md:
> реюз qdrant/docling/neo4j/Caddy, ядро (couchdb/tei/tgapi/боты) — только
> наш compose.

## Целевое состояние

- Docker активен; стек из `vps/docker-compose.yml` поднят;
- CouchDB отвечает, doc_count > 0;
- CLI-цикл (systemd) активен, циклы без ошибок;
- Зеркало синхронизируется (файлы обновляются);
- Indexer ходит циклами; watchdog активен;
- Диск < 80%.

## Порядок развёртывания с нуля

```bash
# 1. Базовые пакеты (Ubuntu 22.04+)
apt update && apt install -y docker.io docker-compose-plugin git
# (либо официальный docker-ce; docker group для рабочего юзера)

# 2. Скопировать репо на сервер
git clone <repo> /opt/memory-stack && cd /opt/memory-stack/vps

# 3. Заполнить .env из .env.example (КРИТИЧНО: пароли — сильные,
#    токены — из Telegram; passphrase НЕ заполнять — она только на устройствах)
cp .env.example .env && nano .env && chmod 600 .env

# 4. Поднять стек
docker compose up -d

# 5. Настроить systemd-сервис CLI-цикла (см. vps/scripts/) и включить
# 6. Проверка
python3 ../validate/validate_vps.py
```

## Подготовка к будущему neo4j (сделать СЕЙЧАС — включится позже бесшовно)

Графовый слой (neo4j) запланирован следующей версией. Чтобы апдейт был бесшовным,
уже сейчас:

1. **RAM**: возьми сервер 12+ GB (бюджет — vps/README.md) — neo4j добавит ~1–1.5 GiB.
2. **Имя/сеть**: сервис уже заготовлен в `docker-compose.yml` (закомментирован)
   под именем `neo4j` в сети `memnet` → все будущие компоненты ходят на
   `bolt://neo4j:7687` без изменений конфигов.
3. **Volume**: `neo4j-data` зарезервирован (раскомментировать вместе с сервисом).
4. **Пароль**: `NEO4J_PASSWORD` — место в `.env` уже помечено.
5. **Порты**: наружу не публиковать (bolt только в memnet; web 7474 — через
   реверс-прокси с авторизацией, если нужен).
6. **Heap**: 512–1024 MB для личных масштабов (задано в заготовке).

Что сделает апдейт: раскомментировать блок → `NEO4J_PASSWORD` в `.env` →
`docker compose up -d` → отдельный сервис-строитель графа (из зеркала: заметки,
викилинки, бэклинки, теги) — его исходники появятся в новой версии репозитория.

## Компоненты стека (docker-compose.yml)

| Сервис | Зачем | Заметки |
|---|---|---|
| couchdb | синхронизация LiveSync | порт 5984 только внутри сети/через реверс-прокси с авторизацией |
| qdrant | векторный поиск | том на диск, auth через API-ключ |
| tei | эмбеддинги | модель e5-small (лёгкая), 2 GB RAM |
| docling | конвертер PDF/DOCX | общий контейнер, API /v1/convert |
| tgapi | Telegram Bot API локально | нужен для быстрых загрузок медиа |
| tg_bot | watcher каналов | читает .env, пишет в staging |
| tg_media | загрузчик медиа | кэш в volume |
| indexer | семантический индекс | ходит циклами по зеркалу |
| ai-assistant | опциональный ИИ-ассистент | необязательный сосед продового инстанса |
| netguard_ssh22 | SSH rate-limit | privileged, restart=always (см. runbook-ssh-hygiene.md) |

## systemd-сервис CLI-цикла

`/etc/systemd/system/memory-livesync-reconcile.service` (пример в `vps/scripts/`):

```ini
[Unit]
Description=Memory Stack LiveSync reconciliation loop
After=docker.service

[Service]
ExecStart=/opt/memory-stack/vps/scripts/livesync-reconcile-loop.sh
Restart=always
RestartSec=30
PrivateTmp=yes
ProtectSystem=full

[Install]
WantedBy=multi-user.target
```

Ключевой скрипт цикла (`livesync-reconcile-loop.sh`) — порядок ЖЁСТКИЙ:

```bash
# каждые ~30 секунд:
sync(in)      # подтянуть изменения устройств в CLI-БД
sync(out)     # отправить зеркало → CouchDB
mirror        # ПОСЛЕДНИЙ: записать состояние БД на диск зеркала
              # (mirror-last = самовосстановление от deletion-волн)
```

## Ложные тревоги (не чинить)

- Healthcheck CouchDB «unhealthy» — healthcheck без авторизации ловит 401. Норма.
- gotenberg unhealthy — healthcheck зовёт wget, которого нет в образе. Норма.

## Чек-лист после ребута ВМ

1. `systemctl is-active docker memory-livesync-reconcile`
2. Контейнеры подняты (`docker ps`), load спадает;
3. CLI-цикл: последний цикл OK (лог в `/tmp/reconcile-cli.log`, у сервиса PrivateTmp —
   читать через `/proc/$(pgrep -f livesync-reconcile-loop)/root/tmp/`);
4. netguard_ssh22 применил правила;
5. Внешние сервисы (couch через домен) отвечают.

## Восстановление из снапшота (Hostinger/прочее)

- Снапшот = полный образ ВМ; восстановление необратимо и блокирует ВМ ~30 мин;
- После отката: сменится SSH host key (`ssh-keygen -R <ip>`), возможен транзиентный
  read-only FS, устройства потребуют re-setup (см. runbook-obsidian-livesync.md).
