#!/bin/bash
# Автосинк SSH-whitelist с публичным IP рабочей машины.
#
# Зачем: машина может ходить через VPN с разными выходными IP → статический
# whitelist перестаёт работать. Канари (pc-scripts/health_canary.py) публикует
# текущий IP в заметке Inbox/AI/Status/health.md; этот скрипт (cron */10)
# читает заметку из зеркала и идемпотентно добавляет ACCEPT-правило.
# Старые IP (TTL) вычищаются.
#
# Требование: заметка канари должна доезжать до зеркала (LiveSync → CouchDB → mirror).
set -uo pipefail

NOTE="/srv/memory-system/vault/memory-ai/Inbox/AI/Status/health.md"
STATE="/opt/memory-stack/prod/whitelist_ips.txt"
TTL_DAYS=7
MAX_IPS=5
LOG="/var/log/memory-stack/whitelist-sync.log"

mkdir -p "$(dirname "$LOG")"
log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$LOG"; }

[ -f "$NOTE" ] || { log "заметка канари не найдена: $NOTE"; exit 0; }

IP=$(grep -a -m1 -E 'Публичный IP' "$NOTE" | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' | head -1)
if [ -z "${IP:-}" ]; then log "IP не найден в отчёте"; exit 0; fi

NOW=$(date +%s)
touch "$STATE"

if grep -q "^${IP} " "$STATE"; then
  sed -i "s|^${IP} .*|${IP} ${NOW}|" "$STATE"
else
  echo "${IP} ${NOW}" >> "$STATE"
  log "новый IP в whitelist: ${IP}"
fi

mapfile -t LINES < "$STATE"
KEEP=()
for line in "${LINES[@]}"; do
  [ -z "$line" ] && continue
  ts=$(echo "$line" | awk '{print $2}')
  age_days=$(( (NOW - ts) / 86400 ))
  if [ "$age_days" -lt "$TTL_DAYS" ]; then KEEP+=("$line"); else log "IP устарел: $line"; fi
done
if [ "${#KEEP[@]}" -gt "$MAX_IPS" ]; then KEEP=("${KEEP[@]:0:$MAX_IPS}"); fi
printf '%s\n' "${KEEP[@]}" > "$STATE"

for line in "${KEEP[@]}"; do
  [ -z "$line" ] && continue
  ip=$(echo "$line" | awk '{print $1}')
  if ! docker exec netguard_ssh22 iptables -C INPUT -s "$ip" -p tcp --dport 22 -j ACCEPT 2>/dev/null; then
    if docker exec netguard_ssh22 iptables -I INPUT 1 -s "$ip" -p tcp --dport 22 -j ACCEPT 2>/dev/null; then
      log "добавлено правило ACCEPT для ${ip}"
    else
      log "не удалось добавить правило для ${ip}"
    fi
  fi
done

for spec in $(docker exec netguard_ssh22 iptables -S INPUT 2>/dev/null | grep -- '-j ACCEPT' | grep 'dpt:22' | sed 's/^-A INPUT //'); do
  ip=$(echo "$spec" | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' | head -1)
  [ -z "$ip" ] && continue
  if ! grep -q "^${ip} " "$STATE"; then
    docker exec netguard_ssh22 iptables -D INPUT $spec 2>/dev/null && log "удалено устаревшее правило для ${ip}"
  fi
done

log "итог: $(wc -l < "$STATE") IP в whitelist, текущий=${IP}"
