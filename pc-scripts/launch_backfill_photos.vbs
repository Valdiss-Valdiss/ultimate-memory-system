' Бэкфилл фото (скрытое окно, 3 воркера). Идемпотентно; держится живым hourly-guard'ом.
Set sh = WScript.CreateObject("WScript.Shell")
cmd = "cmd /c cd /d C:\Users\<user>\memory-stack\scripts\telegram-import && python photos_backfill.py > NUL 2>&1"
sh.Run cmd, 0, False
