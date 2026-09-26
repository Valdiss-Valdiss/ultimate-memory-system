$ErrorActionPreference = 'SilentlyContinue'
$dir = 'C:\projects\opencode\Memory_System\scripts\telegram-import'

# --- media backfill (Deepgram) - same behavior as before ---
$proc = Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match 'media_backfill' }
if ($proc) {
  Add-Content -Path "$dir\catchup.log" -Value ("[backfill_autorestart] media already running (PID " + ($proc.ProcessId -join ',') + "), skip")
} else {
  Add-Content -Path "$dir\catchup.log" -Value "[backfill_autorestart] media not running - reset 212 (if needed) and start"
  & C:\Python314\python.exe "$dir\reset_212.py" >> "$dir\catchup.log" 2>&1
  Start-Process -FilePath 'C:\Python314\python.exe' -ArgumentList 'media_backfill.py' -WorkingDirectory $dir -RedirectStandardError "$dir\media_stderr.log" -WindowStyle Hidden
}

# --- docs+photos backfill (2026-09-13, attachment converter, one process) ---
# PAUSED 2026-09-13 evening: Telegram HTTP 429 rate-limit on the user session.
# RE-ENABLED 2026-09-13 late evening: mini-test passed (single client, 2 requests),
# hourly task re-enabled. Keep discipline: ONE client only, moderate pace.
$attPause = $false
if (-not $attPause) {
  Start-Sleep -Seconds 25
  $att = Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match 'attachments_backfill' }
  if ($att) {
    Add-Content -Path "$dir\catchup.log" -Value ("[backfill_autorestart] attachments already running (PID " + ($att.ProcessId -join ',') + "), skip")
  } else {
    Add-Content -Path "$dir\catchup.log" -Value "[backfill_autorestart] attachments_backfill not running - start"
    Start-Process -FilePath 'C:\Python314\python.exe' -ArgumentList 'attachments_backfill.py' -WorkingDirectory $dir -RedirectStandardError "$dir\backfill_stderr.log" -WindowStyle Hidden
  }
} else {
  Add-Content -Path "$dir\catchup.log" -Value "[backfill_autorestart] attachments PAUSED (cpu quota) - skip"
}
