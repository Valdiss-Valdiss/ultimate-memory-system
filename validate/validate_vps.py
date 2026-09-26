# -*- coding: utf-8 -*-
"""validate_vps.py — проверка целевого состояния сервера Memory System.

Запуск на сервере:
  python3 validate_vps.py --db-user USER --db-pass PASS [--db-name NAME] \
      [--mirror-dir DIR]
Коды выхода: 0 — всё зелёное; N — число FAIL.
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request

PASS, FAIL = [], []


def ok(msg):
    PASS.append(msg)
    print('[OK]  ', msg)


def fail(msg, hint=''):
    FAIL.append(msg)
    print('[FAIL]', msg, (f'→ {hint}' if hint else ''))


def warn(msg):
    print('[WARN]', msg)


def sh(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, '', str(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db-user', default=os.environ.get('COUCHDB_USER', ''))
    ap.add_argument('--db-pass', default=os.environ.get('COUCHDB_PASSWORD', ''))
    ap.add_argument('--db-name', default=os.environ.get('COUCH_DB_NAME', ''))
    ap.add_argument('--mirror-dir', default=os.environ.get('MIRROR_DIR',
                   '/srv/memory-system/vault/memory-ai'))
    args = ap.parse_args()

    # 1. docker
    rc, out, _ = sh(['systemctl', 'is-active', 'docker'])
    (ok if out.strip() == 'active' else fail)('docker active')

    # 2. контейнеры
    rc, out, _ = sh(['docker', 'ps', '--format', '{{.Names}} {{.Status}}'])
    mem = [l for l in out.splitlines() if l.startswith(('memory_', 'netguard'))]
    up = [l for l in mem if ' Up ' in l or l.split(None, 1)[1].startswith('Up')]
    (ok if len(up) == len(mem) else fail)(
        f'контейнеры стека Up: {len(up)}/{len(mem)}')

    # 3. CouchDB через docker exec
    if args.db_user and args.db_pass:
        cmd = ['docker', 'exec', 'memory_couchdb', 'curl', '-s',
               '-u', f'{args.db_user}:{args.db_pass}',
               f'http://localhost:5984/{args.db_name}']
        rc, out, _ = sh(cmd, timeout=30)
        try:
            d = json.loads(out)
            dc, dd = d.get('doc_count', -1), d.get('doc_del_count', -1)
            (ok if dc > 0 else fail)(f'CouchDB doc_count={dc}')
            (ok if dd == 0 else fail)(
                f'CouchDB doc_del_count={dd}',
                'возможна deletion-волна — runbook-deletion-waves.md')
        except Exception:
            fail('CouchDB: не удалось прочитать (креды/имя базы?)')
    else:
        warn('CouchDB пропущен (--db-user/--db-pass не заданы)')

    # 4. CLI-цикл
    rc, out, _ = sh(['systemctl', 'is-active', 'memory-livesync-reconcile'])
    (ok if out.strip() == 'active' else fail)('CLI-цикл active')
    rc, out, _ = sh(['pgrep', '-f', 'livesync-reconcile-loop'])
    pid = (out.strip().splitlines() or [''])[0]
    if pid:
        rc, log, _ = sh(['tail', '-c', '300000',
                         f'/proc/{pid}/root/tmp/reconcile-cli.log'])
        errs = sum(1 for l in log.splitlines() if l.startswith('[Error]'))
        (ok if errs == 0 else fail)(f'лог цикла ошибок: {errs}',
                                    'runbook-vps-setup.md')
        (ok if 'Synchronisation completed' in log else fail)(
            'зеркальные циклы завершаются')
    else:
        fail('процесс CLI-цикла не найден')

    # 5. зеркало
    if os.path.isdir(args.mirror_dir):
        n = sum(len([f for f in fs if f.endswith('.md')])
                for _, _, fs in os.walk(args.mirror_dir))
        (ok if n > 0 else fail)(f'зеркало: {n} .md')
    else:
        fail(f'зеркало не найдено: {args.mirror_dir}')

    # 6. ресурсы
    load = os.getloadavg()[0]
    cpus = os.cpu_count() or 1
    (ok if load < cpus * 2 else fail)(f'load {load:.1f} / cpus {cpus}')
    rc, out, _ = sh(['df', '/', '-P'])
    try:
        use = int(out.splitlines()[1].split()[4].rstrip('%'))
        (ok if use < 80 else fail)(f'диск {use}%')
    except Exception:
        warn('диск не прочитан')

    print('──────────────────────────')
    print(f'ИТОГО: OK={len(PASS)} FAIL={len(FAIL)}')
    sys.exit(len(FAIL))


if __name__ == '__main__':
    main()
