#!/usr/bin/env bash
# Вотчдог конвейера: если Bot API получает посты (offset свежий), а вотчер
# не создавал заметок 2 часа (нет "[+]" в его логе) — считаем оглохшим
# (инцидент 05.09.2026) и рестартим. Крон */10; рестарт не чаще 1 раза в час.
# 2026-10-25: добавлен блок простых контейнеров (tgapi/media) — если контейнер
# СТОИТ (умер/не поднялся после ребута VM — известная проблема Hostinger),
# поднимаем сразу, не дожидаясь признаков оглохшего вотчера (решение юзера:
# самовосстановление конвейера).
LOG=/opt/memory-stack/tg-bot/logs/watchdog.log
OFFSET=/opt/memory-stack/tg-bot/state/media_offset.json
NOW=$(date +%s)

# 0. Стоящие контейнеры конвейера -> немедленный подъем (не чаще 1 раза в час на контейнер)
for C in memory_tgapi memory_tg_media; do
    if [ "$(docker inspect -f '{{.State.Running}}' "$C" 2>/dev/null)" != "true" ]; then
        if [ -f "$LOG" ]; then
            log_age=$(( NOW - $(stat -c %Y "$LOG" 2>/dev/null || echo 0) ))
            [ "$log_age" -lt 3600 ] && continue
        fi
        echo "$(date -u '+%FT%TZ') контейнер $C стоит -> старт" >> "$LOG"
        docker start "$C" >> "$LOG" 2>&1
        continue
    fi
done

# 1. Bot API-путь жив? (offset обновлялся за последние 45 мин — есть посты)
[ -f "$OFFSET" ] || exit 0
off_age=$(( NOW - $(stat -c %Y "$OFFSET" 2>/dev/null || echo "$NOW") ))
[ "$off_age" -lt 2700 ] || exit 0

# 2. Вотчер молчит? Логирование "[+]" — единственный честный признак:
#    staging-файлы эфемерны (обогатитель забирает через ~5 мин), mirror
#    обновляется и от редакций пользователя — не годится как сигнал.
if docker logs memory_tg_bot --since 2h 2>&1 | grep -q "^\[+\]"; then
    exit 0
fi

# 3. Не рестартали ли в последний час?
if [ -f "$LOG" ]; then
    log_age=$(( NOW - $(stat -c %Y "$LOG" 2>/dev/null || echo 0) ))
    [ "$log_age" -lt 3600 ] && exit 0
fi

echo "$(date -u '+%FT%TZ') tg_bot молчит при живом Bot API (offset age=${off_age}s) -> рестарт" >> "$LOG"
docker restart memory_tg_bot >> "$LOG" 2>&1
