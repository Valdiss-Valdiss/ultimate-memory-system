# -*- coding: utf-8 -*-
"""validate_pc.py — проверка целевого состояния ПК-конвейера (Windows).

Запуск: py -3 validate_pc.py
Коды выхода: 0 — всё зелёное; N — число FAIL.
"""
import glob
import os
import subprocess
import sys
import time

PASS, FAIL, WARN = [], [], []


def ok(msg):
    PASS.append(msg)
    print('[OK]  ', msg)


def fail(msg, hint=''):
    FAIL.append(msg)
    print('[FAIL]', msg, (f'→ {hint}' if hint else ''))


def warn(msg):
    print('[WARN]', msg)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    scripts = os.path.join(os.path.dirname(here), 'pc-scripts')

    # 1. Obsidian
    rc = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq Obsidian.exe'],
                        capture_output=True, text=True)
    (ok if 'obsidian.exe' in rc.stdout.lower() else fail)(
        'Obsidian запущен',
        'внешние записи в vault запрещены при закрытом Obsidian')

    # 2. python-процессы конвейера (не более одного!)
    rc = subprocess.run(['powershell', '-NoProfile', '-Command',
                         'Get-CimInstance Win32_Process -Filter "Name=\'python.exe\'"'
                         ' | Select-Object -ExpandProperty CommandLine'],
                        capture_output=True, text=True)
    conv = [l for l in (rc.stdout or '').splitlines()
            if 'catchup' in l or 'backfill' in l or 'renamer' in l]
    if len(conv) <= 1:
        ok(f'Telegram-клиентов: {len(conv)} (норма ≤1)')
    else:
        fail(f'Telegram-клиентов: {len(conv)}', 'одно-клиентное правило нарушено')

    # 3. floodguard
    marker = os.path.join(scripts, 'tg_cooldown_until.txt')
    if os.path.exists(marker):
        try:
            until = float(open(marker, encoding='utf-8').read().strip())
            if until > time.time():
                warn(f'floodguard: охлаждение ещё {int((until - time.time()) / 60)} мин')
            else:
                ok('floodguard: маркер истёк')
        except Exception:
            ok('floodguard: маркер не читается (считаем истёкшим)')
    else:
        ok('floodguard: маркера нет')

    # 4. питание/сон (Windows)
    rc = subprocess.run(['powercfg', '/query', 'SCHEME_CURRENT',
                         'SUB_SLEEP', 'STANDBYIDLE'],
                        capture_output=True, text=True)
    if '0x00000000' in rc.stdout:
        ok('сон по таймеру: никогда')
    else:
        fail('сон по таймеру включён',
             'runbook-windows-quirks.md §1 (ночные конвейеры будут умирать)')
    rc = subprocess.run(['powercfg', '/query', 'SCHEME_CURRENT',
                         'SUB_SLEEP', 'UNATTENDSLEEP'],
                        capture_output=True, text=True)
    if rc.stdout.strip() and '0x00000000' in rc.stdout:
        ok('unattended sleep: никогда')
    else:
        fail('unattended sleep ≠ 0',
             'планировщик будит ПК → 2 мин → standby (главный убийца ночей)')

    # 5. vault
    vault = os.environ.get('MEMORY_VAULT_DIR',
                           r'C:\Users\<user>\Documents\Obsidian Vault')
    if os.path.isdir(vault):
        n = sum(1 for _, _, fs in os.walk(vault) for f in fs
                if f.lower().endswith('.md'))
        (ok if n > 0 else fail)(f'vault: {n} .md ({vault})')
    else:
        fail(f'vault не найден: {vault}', 'задай MEMORY_VAULT_DIR')

    # 6. stderr-файлы конвейера: трейсбеки за сегодня?
    for name in ('backfill_stderr.log', 'media_stderr.log'):
        p = os.path.join(scripts, name)
        if os.path.exists(p):
            sz = os.path.getsize(p)
            (ok if sz < 100000 else fail)(
                f'{name}: {sz} байт',
                'traceback\'и в stderr — смотреть первым делом')

    # 7. часовая задача
    rc = subprocess.run(['schtasks', '/Query', '/TN', 'Memory Catchup Hourly'],
                        capture_output=True, text=True)
    (ok if rc.returncode == 0 and 'Ready' in rc.stdout else fail)(
        'задача Memory Catchup Hourly',
        'pc-scripts/README.md — установка планировщика')

    print('──────────────────────────')
    print(f'ИТОГО: OK={len(PASS)} FAIL={len(FAIL)} WARN={len(WARN)}')
    sys.exit(len(FAIL))


if __name__ == '__main__':
    main()
