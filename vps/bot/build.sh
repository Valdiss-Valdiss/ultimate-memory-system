#!/usr/bin/env bash
# build.sh — собрать исходники конвейера из pc-scripts и построить образ
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

# 1. скопировать исходники .py из pc-scripts (единый источник правды по коду)
cp -f "$DIR"/../../pc-scripts/*.py "$DIR"/

# 2. folders.yaml: реальный файл коммитить нельзя; если нет — создать болванку
if [ ! -f "$DIR/folders.yaml" ]; then
  cat > "$DIR/folders.yaml" <<'EOF'
# Маппинг Telegram-каналов → целевых папок vault.
# Ключ = точное название канала; значение = папка назначения.
# Горячая перезагрузка: правится в любой момент.
# Пример:
# .it: "IT/"
EOF
fi

# 3. сборка
docker build -t memory-tg-tools:latest "$DIR"
echo "Образ memory-tg-tools:latest собран."
