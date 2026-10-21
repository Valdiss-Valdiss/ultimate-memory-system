' Бэкфилл вложений: документы → фото (последовательно, скрытое окно).
' Идемпотентно: можно перезапускать, скрипты сами находят необработанные цели.
Set sh = WScript.CreateObject("WScript.Shell")
cmd = "cmd /c cd /d C:\Users\<user>\memory-stack\scripts\telegram-import && " & _
      "python docs_backfill.py > NUL 2>&1 && python photos_backfill.py > NUL 2>&1"
sh.Run cmd, 0, False
