# systemd: установка CLI-цикла (livesync-reconcile)

## 1. Скрипт цикла

`/opt/memory-stack/vps/scripts/livesync-reconcile-loop.sh` (пример):

```bash
#!/usr/bin/env bash
set -u
cd /opt/memory-stack/vps
while true; do
  ts=$(date '+%F %T')
  echo "[$ts] cycle start" >> /tmp/reconcile-cli.log
  bash livesync-cli-once.sh sync          >> /tmp/reconcile-cli.log 2>&1 || true
  bash livesync-cli-once.sh sync          >> /tmp/reconcile-cli.log 2>&1 || true
  bash livesync-cli-once.sh mirror /vault >> /tmp/reconcile-cli.log 2>&1 || true
  sleep 30
done
```

ВНИМАНИЕ: третий шаг — `mirror` — всегда ПОСЛЕДНИЙ (runbook-deletion-waves.md).
У CLI-CLI (obsidian-livesync CLI) собственные параметры — указать настройки БД
и путь зеркала (см. документацию плагина Self-hosted LiveSync, режим CLI).

## 2. Юнит

`/etc/systemd/system/memory-livesync-reconcile.service`:

```ini
[Unit]
Description=Memory Stack LiveSync reconciliation loop
After=docker.service network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/env bash /opt/memory-stack/vps/scripts/livesync-reconcile-loop.sh
Restart=always
RestartSec=30
PrivateTmp=yes
ProtectSystem=full

[Install]
WantedBy=multi-user.target
```

- `PrivateTmp=yes` — лог цикла живёт в /tmp сервиса; читать через
  `/proc/$(pgrep -f livesync-reconcile-loop)/root/tmp/reconcile-cli.log`.
- `ProtectSystem=full` — /opt для сервиса read-only; записываемые файлы — в /tmp.

## 3. Включение

```bash
systemctl daemon-reload
systemctl enable --now memory-livesync-reconcile
systemctl status memory-livesync-reconcile --no-pager
```

## 4. Проверка

```bash
tail -c 4000 /proc/$(pgrep -f livesync-reconcile-loop)/root/tmp/reconcile-cli.log
# ожидать: «Synchronisation completed: N/N files processed successfully»
```
