@echo off
rem 2026-11-07: single entry point. At logon run the scheduled task "Memory Catchup Hourly"
rem (its policies apply: 30 min limit, stop-existing). Previously this ran catchup.bat directly,
rem bypassing the task -> two catchups could share one Telegram session (database is locked).
rem Fallback: if the task cannot be started, run the hourly chain directly.
timeout /t 90 /nobreak >nul
schtasks /run /tn "Memory Catchup Hourly" >nul 2>&1
if errorlevel 1 call "C:\Users\<user>\memory-stack\scripts\telegram-import\catchup_hourly.bat"
