@echo off
cd /d "C:\projects\opencode\Memory_System\scripts\telegram-import"
"C:\Python314\python.exe" pc_catchup.py >> catchup.log 2>&1
"C:\Python314\python.exe" triage_folders.py >> catchup.log 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\projects\opencode\Memory_System\scripts\telegram-import\backfill_autorestart.ps1" >> catchup.log 2>&1
rem note_renamer: безымянные заметки -> осмысленные имена по содержимому фото/документа
"C:\Python314\python.exe" note_renamer.py --execute >> catchup.log 2>&1


