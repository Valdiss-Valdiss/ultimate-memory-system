' Часовой догон (скрытый запуск catchup_hourly.bat)
' 2026-10-25: bWaitOnReturn=True — задача ждёт ВЕСЬ прогон; тогда IgnoreNew/
' StopExisting/ExecutionTimeLimit задачи реально действуют (раньше wscript
' выходил мгновенно → зомби-прогоны никто не замечал и не убивал)
Set sh = CreateObject("WScript.Shell")
sh.Run """C:\Users\<user>\memory-stack\scripts\telegram-import\catchup_hourly.bat""", 0, True
