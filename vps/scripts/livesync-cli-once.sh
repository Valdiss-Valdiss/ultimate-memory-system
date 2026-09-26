#!/usr/bin/env bash
# livesync-cli-once.sh — обёртка одиночного вызова LiveSync CLI (только чтение режима CLI)
# Использование: livesync-cli-once.sh <sync|mirror|rm|pull|push> [args]
# Требует настроенный obsidian-livesync CLI (database-режим) и каталоги:
#   DATABASE=/data (LevelDB CLI), VAULT=/vault (зеркало)

set -euo pipefail

IMAGE="obsidian-livesync-cli:latest"   # собрать по инструкции плагина (mode CLI)
DATABASE="/opt/memory-stack/database"
VAULT="/srv/memory-system/vault/memory-ai"
TIMEOUT_SECS="${TIMEOUT_SECS:-180}"

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <command> [args...]" >&2
    echo "  examples: $0 sync | $0 mirror /vault | $0 rm 'Path/To/Note.md'" >&2
    exit 1
fi

# Отказ при активном параллельном экземпляре
if docker container ls --format '{{.Names}}' 2>/dev/null | grep -q '^livesync-cli-prod$'; then
    echo "[FATAL] livesync-cli-prod is running. Refusing." >&2
    exit 2
fi

set +e
timeout --kill-after=10 "${TIMEOUT_SECS}" \
    docker run --rm \
        --cpus 1.0 \
        --user 1001:1001 \
        -e HOME=/tmp \
        -v "${DATABASE}:/data" \
        -v "${VAULT}:/vault" \
        "${IMAGE}" \
        --settings /data/settings.json \
        --vault /vault \
        "$@"
status=$?
set -e

# Проверить, что LevelDB отпущен
sleep 2
LEVELDB_HOLDER=$(lsof +t "${DATABASE}" 2>/dev/null | head -1 || true)
if [[ -n "${LEVELDB_HOLDER}" ]]; then
    echo "[WARN] LevelDB still held by PID ${LEVELDB_HOLDER}" >&2
fi

exit ${status}
