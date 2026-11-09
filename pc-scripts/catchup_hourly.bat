@echo off
rem Часовая цепочка: догон, прогоняемый планировщиком (ежечасно).
rem 2026-10-25: guard (backfill_autorestart.ps1) и его долгоживущие python-дети
rem переведены на guard.log — раньше они наследовали эксклюзивный >>-хэндл cmd
rem и запирали catchup.log на часы (все прогоны молча теряли вывод; инцидент
rem 20.10 и сегодняшняя серия). Короткие шаги пишут в catchup.log как раньше.
cd /d "C:\Users\<user>\memory-stack\scripts\telegram-import"
rem 2026-11-07: Python output in UTF-8 (was cp1251 mojibake); rotate logs > 20 MB (keep one .prev.log)
set PYTHONIOENCODING=utf-8
for %%F in (catchup.log guard.log canary.log) do if exist %%F for %%A in (%%F) do if %%~zA GTR 20000000 move /y %%F %%~nF.prev.log >nul
echo [%date% %time%] === hourly catchup === >> catchup.log
"python" pc_catchup.py >> catchup.log 2>&1
"python" triage_folders.py >> catchup.log 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\<user>\memory-stack\scripts\telegram-import\backfill_autorestart.ps1" >> guard.log 2>&1
"python" note_renamer.py --execute >> catchup.log 2>&1
