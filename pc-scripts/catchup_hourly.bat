@echo off
rem Часовой догон: посты, пропущенные вотчером (страховочный контур).
cd /d "C:\Users\<user>\memory-stack\scripts\telegram-import"
echo [%date% %time%] === hourly catchup === >> catchup.log
"python" pc_catchup.py >> catchup.log 2>&1
"python" triage_folders.py >> catchup.log 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\<user>\memory-stack\scripts\telegram-import\backfill_autorestart.ps1" >> catchup.log 2>&1
"python" note_renamer.py --execute >> catchup.log 2>&1
