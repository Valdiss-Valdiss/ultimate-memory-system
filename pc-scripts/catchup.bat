@echo off
cd /d "C:\Users\<user>\memory-stack\scripts\telegram-import"
"python" pc_catchup.py >> catchup.log 2>&1
"python" triage_folders.py >> catchup.log 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\<user>\memory-stack\scripts\telegram-import\backfill_autorestart.ps1" >> catchup.log 2>&1
rem note_renamer: безымянные заметки -> осмысленные имена по содержимому фото/документа
"python" note_renamer.py --execute >> catchup.log 2>&1


