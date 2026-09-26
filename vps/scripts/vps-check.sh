#!/usr/bin/env bash
# vps-check.sh — комплексный healthcheck Memory System (только чтение)
# Использование: bash vps-check.sh [COUCH_USER] [COUCH_PASSWORD]
# Выход: список проверок с статусом [OK]/[FAIL] + сводка.

set -u
PASS=0; FAIL=0

ok()   { echo "[OK]   $1"; PASS=$((PASS+1)); }
fail() { echo "[FAIL] $1"; FAIL=$((FAIL+1)); }

# 1. docker активен
systemctl is-active docker >/dev/null 2>&1 && ok "docker active" || fail "docker not active"

# 2. контейнеры стека
DEAD=$(docker ps -a --format '{{.Names}} {{.Status}}' | grep -E '^(memory_|netguard)' | grep -cvE 'Up' || true)
UP=$(docker ps --format '{{.Names}}' | grep -cE '^(memory_|netguard)' || true)
[ "$DEAD" = "0" ] && ok "все memory-контейнеры Up ($UP шт)" || fail "контейнеров не в Up: $DEAD"

# 3. рестарты за последние часы (штатный watchdog не считается)
BAD_RESTARTS=$(for c in $(docker ps --format '{{.Names}}' | grep '^memory_'); do
  S=$(docker inspect "$c" --format '{{.State.StartedAt}}' | cut -c1-10)
  R=$(docker inspect "$c" --format '{{.RestartCount}}')
  echo "$c $R"; done | awk '$2 > 3' | wc -l)
[ "$BAD_RESTARTS" = "0" ] && ok "рестартов > 3 нет" || fail "контейнеров с аномальными рестартами: $BAD_RESTARTS"

# 4. CouchDB (если переданы креды)
DB_NAME="${COUCH_DB_NAME:-}"
if [ -n "${1:-}" ] && [ -n "${2:-}" ]; then
  INFO=$(docker exec memory_couchdb curl -s -u "$1:$2" "http://localhost:5984/$DB_NAME" 2>/dev/null)
  DC=$(echo "$INFO" | python3 -c "import sys,json; print(json.load(sys.stdin).get('doc_count',-1))" 2>/dev/null || echo -1)
  DD=$(echo "$INFO" | python3 -c "import sys,json; print(json.load(sys.stdin).get('doc_del_count',-1))" 2>/dev/null || echo -1)
  [ "$DC" -gt 0 ] && ok "CouchDB doc_count=$DC" || fail "CouchDB doc_count=$DC"
  [ "$DD" = "0" ] && ok "CouchDB doc_del_count=0" || fail "CouchDB doc_del_count=$DD (проверить deletion-волну!)"
else
  echo "[SKIP] CouchDB (передай креды аргументами)"
fi

# 5. CLI-цикл
systemctl is-active memory-livesync-reconcile >/dev/null 2>&1 && ok "CLI-цикл active" || fail "CLI-цикл не active"
PID=$(pgrep -f livesync-reconcile-loop | head -1)
if [ -n "$PID" ]; then
  ERRS=$(tail -c 300000 /proc/$PID/root/tmp/reconcile-cli.log 2>/dev/null | grep -ciE "^(\[Error\]|error:)" || true)
  [ "${ERRS:-0}" = "0" ] && ok "лог цикла: 0 ошибок (свежие 300KB)" || fail "лог цикла: ошибок $ERRS"
  MIRROR_OK=$(tail -c 4000 /proc/$PID/root/tmp/reconcile-cli.log 2>/dev/null | grep -c "Synchronisation completed" || true)
  [ "$MIRROR_OK" -gt 0 ] && ok "зеркальные циклы завершаются" || fail "зеркальных циклов не видно"
else
  fail "процесс CLI-цикла не найден"
fi

# 6. зеркало: свежесть
MIRROR_DIR="${MIRROR_DIR:-/srv/memory-system/vault/memory-ai}"
CNT=$(find "$MIRROR_DIR" -name '*.md' 2>/dev/null | wc -l)
[ "$CNT" -gt 0 ] && ok "зеркало: $CNT .md" || fail "зеркало пустое/недоступно"
RECENT=$(find "$MIRROR_DIR" -name '*.md' -mmin -60 2>/dev/null | wc -l)
[ "$RECENT" -gt 0 ] && ok "зеркало живое (изменения за час)" || echo "[WARN] зеркало: нет изменений за час (может быть тишина)"

# 7. indexer
IE=$(docker logs memory_indexer --since 2h 2>&1 | grep -ciE "error|traceback" || true)
[ "${IE:-0}" = "0" ] && ok "indexer: ошибок нет (2h)" || fail "indexer: ошибок $IE"

# 8. ресурсы
LOAD=$(awk '{print int($1)}' /proc/loadavg)
CPUS=$(nproc)
[ "$LOAD" -lt $((CPUS*2)) ] && ok "load $LOAD / cpus $CPUS" || fail "load $LOAD перегружен"
DF=$(df / | awk 'NR==2{print int($5)}')
[ "$DF" -lt 80 ] && ok "диск $DF%" || fail "диск $DF%"

echo "──────────────────────────────"
echo "ИТОГО: OK=$PASS FAIL=$FAIL"
[ "$FAIL" = "0" ] && echo "СЕРВЕР ЗДОРОВ" || echo "ЕСТЬ ПРОБЛЕМЫ — см. runbooks"
exit $FAIL
