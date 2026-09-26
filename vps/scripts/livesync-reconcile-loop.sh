#!/usr/bin/env bash
# livesync-reconcile-loop.sh — главный цикл. ПОРЯДОК ШАГОВ МЕНЯТЬ НЕЛЬЗЯ.
# sync(in) → sync(out) → mirror (ПОСЛЕДНИЙ) — самовосстановление от deletion-волн
# (runbook-deletion-waves.md). Интервал 30 с.

set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG=/tmp/reconcile-cli.log
INTERVAL="${INTERVAL:-30}"

while true; do
  ts=$(date '+%F %T')
  echo "[$ts] === cycle start ===" >> "$LOG"
  # 1. подтянуть изменения устройств
  bash "$DIR/livesync-cli-once.sh" sync  >> "$LOG" 2>&1 || echo "[$ts] sync(in) rc=$?" >> "$LOG"
  # 2. отправить зеркало → БД
  bash "$DIR/livesync-cli-once.sh" sync  >> "$LOG" 2>&1 || echo "[$ts] sync(out) rc=$?" >> "$LOG"
  # 3. ЗАПИСАТЬ состояние БД на диск зеркала — ПОСЛЕДНИЙ шаг
  bash "$DIR/livesync-cli-once.sh" mirror "$VAULT_PATH" >> "$LOG" 2>&1 \
    || echo "[$ts] mirror rc=$?" >> "$LOG"
  sleep "$INTERVAL"
done
