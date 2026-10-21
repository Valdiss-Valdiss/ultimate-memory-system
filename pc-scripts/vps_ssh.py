# -*- coding: utf-8 -*-
"""Постоянное SSH-соединение к VPS через paramiko: один коннект на весь прогон,
keepalive 15 с, автопереподключение при обрыве. Заменяет пачку ssh/scp
subprocess-вызовов (5-6 новых подключений на каждый файл) — снимает
netguard rate-limit (9 новых/60 с) и шум MaxStartups от ботов.

Ключ/хост те же, что в ssh-командах проекта (read-only доступ к файлу ключа).
На сервере ничего не меняется — только exec/sftp.
"""
import os
import posixpath
import threading
import time

import paramiko

HOST = "CHANGE-ME-server-ip"
USER = "ai-agent"
KEY_PATH = r"C:\Users\<user>\.ssh\id_ed25519"

_lock = threading.Lock()
_client = None


def _connect():
    last = None
    for i in range(12):
        try:
            cli = paramiko.SSHClient()
            cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            cli.connect(HOST, username=USER, key_filename=KEY_PATH,
                        timeout=20, banner_timeout=30, auth_timeout=30,
                        look_for_keys=False, allow_agent=False)
            t = cli.get_transport()
            if t:
                t.set_keepalive(15)
            global _client
            _client = cli
            return cli
        except Exception as e:
            last = e
            time.sleep(15 + (i % 5) * 5)
    raise last


def _get():
    global _client
    with _lock:
        if _client is not None:
            t = _client.get_transport()
            if t and t.is_active():
                return _client
            try:
                _client.close()
            except Exception:
                pass
            _client = None
        return _connect()


def _reset():
    global _client
    with _lock:
        if _client:
            try:
                _client.close()
            except Exception:
                pass
        _client = None


def exec_(cmd, timeout=120):
    """(rc, stdout, stderr). Обрыв/таймаут → исключение (вызывающий ловит и делает reset)."""
    cli = _get()
    with _lock:
        _, out, err = cli.exec_command(cmd, timeout=timeout)
        data = out.read().decode("utf-8", errors="replace")
        e = err.read().decode("utf-8", errors="replace")
        rc = out.channel.recv_exit_status()
    return rc, data, e


def put(local_path, remote_dir, remote_name=None):
    """SFTP-передача с ретраями: обрыв линии → сброс → новый коннект → повтор."""
    name = remote_name or os.path.basename(str(local_path))
    last = None
    for i in range(4):
        try:
            cli = _get()
            with _lock:
                sftp = cli.open_sftp()
                try:
                    sftp.put(str(local_path), posixpath.join(remote_dir, name))
                finally:
                    sftp.close()
            return
        except Exception as e:
            last = e
            _reset()
            time.sleep(10 + (i % 3) * 10)
    raise last


def close():
    _reset()
