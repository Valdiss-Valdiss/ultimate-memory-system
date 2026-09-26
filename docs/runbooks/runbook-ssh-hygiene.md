# Runbook — SSH-гигиена сервера

## Целевое состояние

- Вход только по ключам (PasswordAuthentication no — проверить `.conf`-инклуды!);
- root-вход по паролю запрещён;
- Порядок подключений не «задыхается» от ботов (rate-limit на TCP-уровне);
- Ключи пользователей — в `authorized_keys` (1+ строка), права 600/700.

## Подводные камни конфигурации

1. **В `.conf`-инклудах (`/etc/ssh/sshd_config.d/*.conf`) может переопределяться
   PasswordAuthentication** — проверяй ИХ, а не только основной конфиг
   (первое вхождение директивы выигрывает; Include стоит вверху).
2. Проверка фактических настроек: `sshd -T | grep -iE "passwordauth|permitroot"`.

## Шум «banner exchange / Connection aborted» в скриптах

Причина: боты на открытом порту 22 заполняют очередь неаутентифицированных
соединений (MaxStartups) → легитимные подключения отбрасываются на этапе
banner exchange. Боты НЕ угадывают пароль (он уже отключён), только создают шум.

### Фикс: rate-limit на TCP-уровне (проверено)

Три правила iptables ПЕРЕД ufw-цепями (позиции 1–4):

```
1. ACCEPT established/related на dpt:22        # наши сессии не рвутся
2. ACCEPT с IP-адреса клиента (whitelist)      # свой IP всегда проходит
3. NEW подключения к dpt:22 → учёт per-IP (recent)
4. 9-е+ новое подключение за 60с с одного IP → DROP
```

Персистентность: контейнер `netguard_ssh22` (alpine, --privileged --net=host,
--restart=always) перепроверяет правила при каждом старте (идемпотентно, `iptables -C`).

**НЕ делать:**
- правила ВНУТРИ ufw-цепей (ufw reload их сотрёт);
- порог ниже ~8/мин (наши скрипты делают несколько подключений за минуту);
- ControlMaster/ControlPath на Windows — **Win32-OpenSSH их не поддерживает**
  («getsockname failed: Not a socket» — подключения ломаются).

## Ключи и доступы

- Рабочий юзер на сервере — не root (docker-группа для контейнеров);
- Root — только через хостинг-консоль (аварийный доступ мимо SSH);
- Новый ключ: `ssh-keygen -t ed25519`; публичный — в `authorized_keys` сервера.

## Диагностика

```bash
# кто стучится (нужен root):
journalctl -u ssh --since "1 hour ago" | grep -cE "Failed|Invalid"
# фактические порты/коннекты:
ss -tn state established "( sport = :22 )"
# правила на месте?
iptables -L INPUT -n --line-numbers | head -8
```
