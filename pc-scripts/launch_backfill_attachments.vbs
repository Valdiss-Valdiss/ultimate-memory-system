' Бэкфилл вложений: документы (последовательно) + фото (x3) в одном процессе.
' Идемпотентно; держится живым hourly-guard'ом.
Set sh = WScript.CreateObject("WScript.Shell")
cmd = "cmd /c cd /d C:\Users\<user>\memory-stack\scripts\telegram-import && python attachments_backfill.py >> backfill_stdout.log 2>> backfill_stderr.log"
sh.Run cmd, 0, False
