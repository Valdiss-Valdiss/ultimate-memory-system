#!/bin/sh
# Netguard: rate-limit для SSH (порт 22) + whitelist IP рабочей машины.
# Запускается в контейнере netguard_ssh22 (--privileged --net=host).
# Логика идемпотентна: правила пересоздаются при каждом старте.
#
# Порог: 30 новых соединений за 60 секунд с одного IP.
# Почему не 9: рабочая машина может ходить в сеть через разные VPN-выходы
# (смена IP) — жёсткий порог ловил собственные служебные подключения.
# Парольная аутентификация в sshd отключена (ключи), поэтому порог защищает
# не от взлома, а от шума ботов, забивающих очередь неаутентифицированных.
#
# ВАЙТЛИСТ: укажите IP своей машины (или используйте автосинх —
# vps/scripts/sync-whitelist.sh добавляет текущий IP из отчёта канари).
set -e
apk add -q iptables 2>/dev/null || true

R0A="-s YOUR-HOME-IP -p tcp --dport 22 -j ACCEPT"
R0B="-s YOUR-OLD-HOME-IP -p tcp --dport 22 -j ACCEPT"
R1="-p tcp --dport 22 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT"
R2="-p tcp --dport 22 -m conntrack --ctstate NEW -m recent --set --name SSHRATE2"
R3="-p tcp --dport 22 -m conntrack --ctstate NEW -m recent --update --name SSHRATE2 --seconds 60 --hitcount 30 -j DROP"

iptables -C INPUT $R0A 2>/dev/null || iptables -I INPUT 1 $R0A
iptables -C INPUT $R0B 2>/dev/null || iptables -I INPUT 2 $R0B
iptables -C INPUT $R1 2>/dev/null || iptables -I INPUT 3 $R1

# Пересоздаём правила со счётчиком (удаляем старые по номеру строки)
for n in $(iptables -L INPUT --line-numbers -n | grep SSHRATE2 | awk '{print $1}' | sort -rn); do
  iptables -D INPUT $n 2>/dev/null || true
done
iptables -I INPUT 4 $R2
iptables -I INPUT 5 $R3

echo "RULES-APPLIED"
while true; do sleep 3600; done
