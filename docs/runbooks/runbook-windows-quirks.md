# Runbook — Windows-специфика (питание, wscript, stdout, планировщик)

> Собрано на живом проде Windows 11 (Modern Standby). Каждая секция — проверенный
> фикс. Нарушения этих правил вызывали реальные ночные простои.

## 1. Питание/сон: враг №1 фоновых процессов

Симптомы: ночные конвейеры молча умирают (`Cannot send requests while disconnected`),
часовые задачи «пропадают» на часы, Telegram-сессия рвётся.

Причины (совокупно):
- **Unattended sleep timeout = 2 минуты** (дефолт): планировщик будит ПК → 2 мин
  тишины → обратно в standby → сеть подвешена;
- **Батарея**: на батарее Windows уходит в standby агрессивнее, таймеры не спасают;
- Lid close (закрытая крышка ноутбука) = сон всегда (если не переопределено).

### Фиксы (проверенные)

```bat
:: таймер сна — никогда (AC и DC)
powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 0
powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 0
:: таймер сна БЕЗ присмотра — никогда (главный!)
powercfg -attributes SUB_SLEEP 7bc4a2f9-d8fc-4469-b07b-33eb785aaca0 -ATTRIB_HIDE
powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP UNATTENDSLEEP 0
powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP UNATTENDSLEEP 0
powercfg /setactive SCHEME_CURRENT
```

- **Ноутбук — в розетку.** На батарее Modern Standby срабатывает независимо от
  таймеров (процессы замораживаются посреди ssh/сети и просыпаются при подключении
  зарядки — «Power source change» в журнале питания).
- Проверка: `powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE` → 0x00000000.

## 2. wscript-лаунчеры молча умирают

`wscript.exe` из планировщика не может запустить процесс, когда сессия в
Modern Standby/неактивна — падает БЕЗ СЛЕДА (даже stderr не пишется). При активном
юзере — работает. Отсюда «интермиттентные» пропуски запуска фоновых процессов.

**Фикс**: в guard-скриптах запускать python напрямую:
```powershell
Start-Process -FilePath 'C:\Python314\python.exe' -ArgumentList 'script.py' `
  -WorkingDirectory $dir -RedirectStandardError "$dir\script_stderr.log" -WindowStyle Hidden
```

## 3. Сломанный stdout убивает процесс (OSError Errno 22)

Если stdout перенаправлен в файл и handle ломается (Windows-специфика redirected
handles), `print(line, flush=True)` бросает `OSError: [Errno 22]` — и **процесс падает
целиком** (в т.ч. изнутри обработчика исключений).

**Фикс** (уже внесён во все скрипты конвейера):
```python
def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)
    try:
        print(line, flush=True)
    except Exception:
        pass  # stdout может быть сломан — файловый лог продолжится
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
```

Правило: **stderr фоновых процессов всегда в файл** — traceback'и не должны теряться
(«молчаливая смерть» = stderr в NUL).

## 4. ControlMaster/ControlPath не работают на Windows

Win32-OpenSSH не поддерживает мультиплексирование («getsockname failed: Not a socket»,
подключения ломаются). Если в `~/.ssh/config` есть эти опции — **удалить**.
Шум SSH лечится серверной стороной (runbook-ssh-hygiene.md).

## 5. Планировщик задач

- Часовая цепочка — одна задача, содержимое — `catchup.bat` (последовательность);
- Политика «Run only when user is logged on» — чтобы видеть консоль/процессы;
- После Modern Standby задача может «догонять» сдвинутые прогоны (норма);
- Задача не должна иметь «Stop the task if it runs longer than…» для длинных фаз.

## 6. Telegram-сессия sqlite

- Один процесс с сессией за раз (иначе `database is locked`);
- Сессия — файл в `session/`; бэкапить вместе с vault-конфигами;
- При переносе на другой ПК — копировать файл сессии целиком.
