Set shell = CreateObject("WScript.Shell")
shell.Run "cmd /c cd /d ""C:\Users\<user>\memory-stack\scripts\telegram-import"" && python -u deepgram_backfill.py > backfill_run.log 2>&1", 0, False
