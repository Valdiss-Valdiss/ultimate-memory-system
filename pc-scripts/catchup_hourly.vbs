' Часовой догон (скрытый запуск catchup_hourly.bat)
Set sh = CreateObject("WScript.Shell")
sh.Run """C:\Users\<user>\memory-stack\scripts\telegram-import\catchup_hourly.bat""", 0, False
