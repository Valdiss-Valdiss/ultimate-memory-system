"""Deepgram Speech-to-Text: ключи, ротация, транскрибация.

- Ключи берутся из .env: все переменные с именем *deepgram* (Deepgram_api_1,
  Deepgram_api_2, ..., или DEEPGRAM_API_KEY). На каждый ключ лимит пользователя ($200).
- Ротация: 401/402/403 → ключ помечается исчерпанным в state-файле → следующий.
- Модель Nova-3 Multilingual (авто-язык), smart_format + абзацы.
- Возвращает: {"status": "ok"|"no_deepgram_credits", "transcript", "duration_sec",
  "cost_usd", "key_fp"}.
"""
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path

import requests

# requests timeout не прерывает застрявший sendall при большой загрузке —
# глобальный socket timeout прерывает ЛЮБУЮ операцию сокета через 600 сек.
socket.setdefaulttimeout(600)

BASE = "https://api.deepgram.com/v1/listen"
PARAMS = {"model": "nova-3", "language": "multi",
          "smart_format": "true", "paragraphs": "true"}
PRICE_PER_MIN = 0.0052

MIME = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".ogg": "audio/ogg", ".opus": "audio/ogg", ".oga": "audio/ogg",
    ".wav": "audio/wav", ".flac": "audio/flac", ".amr": "audio/amr",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
    ".avi": "video/avi", ".mkv": "video/x-matroska",
}


def load_env(env_file=None):
    path = Path(env_file) if env_file else Path(__file__).resolve().parent / ".env"
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def deepgram_keys(env_file=None):
    env = load_env(env_file)
    # В контейнерах .env-файла нет — ключи приходят через --env-file (os.environ).
    for name, value in os.environ.items():
        if value and "deepgram" in name.lower():
            env.setdefault(name, value)
    found = []
    for name, value in env.items():
        if value and "deepgram" in name.lower():
            found.append((name, value))

    def sort_key(item):
        low = item[0].lower()
        if low == "deepgram_api_key":
            return (0, 0)
        m = re.search(r"(\d+)$", low)
        return (1, int(m.group(1)) if m else 99)

    found.sort(key=sort_key)
    return [v for _, v in found]


def key_fp(key):
    return f"{key[:6]}...{key[-4:]}"


def _load_state(state_path):
    p = Path(state_path) if state_path else Path(__file__).resolve().parent / "deepgram_state.json"
    if p.exists():
        try:
            return p, json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return p, {}
    return p, {}


def _save_state(p, state):
    p.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def mark_exhausted(key, state_path=None, status="exhausted"):
    p, state = _load_state(state_path)
    state[key_fp(key)] = {"status": status, "at": time.strftime("%Y-%m-%d")}
    _save_state(p, state)


def exhausted_fps(state_path=None):
    _, state = _load_state(state_path)
    return {fp for fp, v in state.items()
            if str(v.get("status", "")).startswith(("exhausted", "invalid"))}


def _mime_for(path):
    return MIME.get(Path(path).suffix.lower(), "application/octet-stream")


def compress_audio(path, timeout=600, min_size=1_500_000):
    """Аудио → 16 кГц моно Opus 24 кбит/с (fallback AAC). Мелкие файлы не трогаем.

    Возвращает (путь_к_сжатому, True) или (None, False) — тогда шлём оригинал.
    """
    src = Path(path)
    try:
        if src.stat().st_size < min_size:
            return None, False  # и так мало — не тратим время
    except OSError:
        return None, False
    import shutil

    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    # nice понижает приоритет ffmpeg, чтобы сжатие не душило другие сервисы
    # (на Windows nice нет — идём без него)
    nice = ["nice", "-n", "19"] if shutil.which("nice") else []
    out = src.parent / (src.stem + "_c16.opus")
    for codec in (["-c:a", "libopus", "-b:a", "24k"], ["-c:a", "aac", "-b:a", "32k"]):
        try:
            r = subprocess.run([*nice, exe, "-y", "-i", str(src), "-vn", "-ac", "1",
                                "-ar", "16000", *codec, str(out)],
                               capture_output=True, timeout=timeout)
            if r.returncode == 0 and out.exists() and out.stat().st_size > 1024:
                return out, True
        except Exception:
            continue
    out.unlink(missing_ok=True)
    return None, False


def _parse_response(j):
    try:
        alt = j["results"]["channels"][0]["alternatives"][0]
        paras = (alt.get("paragraphs") or {}).get("paragraphs") or []
        text = "\n\n".join(p.get("text", "").strip() for p in paras if p.get("text", "").strip())
        if not text:
            text = alt.get("transcript", "") or ""
        dur = float((j.get("metadata") or {}).get("duration") or 0)
        return text, dur
    except Exception:
        return "", 0.0


def transcribe_file(path, state_path=None, timeout=(30, 600), log=print):
    """Файл → Deepgram. Ротация ключей. status: ok | no_deepgram_credits | error."""
    path = Path(path)
    try:
        if not path.exists() or path.stat().st_size < 1024:
            return {"status": "error", "transcript": "", "duration_sec": 0, "cost_usd": 0,
                    "key_fp": "", "error": "файл пуст или битый"}
    except OSError as e:
        return {"status": "error", "transcript": "", "duration_sec": 0, "cost_usd": 0,
                "key_fp": "", "error": f"нет доступа к файлу: {e}"}
    keys = deepgram_keys()
    if not keys:
        return {"status": "no_deepgram_credits", "transcript": "", "duration_sec": 0,
                "cost_usd": 0, "key_fp": "", "error": "нет ключей Deepgram в .env"}
    dead = exhausted_fps(state_path)
    last_err = ""
    for key in keys:
        fp = key_fp(key)
        if fp in dead:
            continue
        for attempt in range(3):
            try:
                with open(path, "rb") as f:
                    r = requests.post(BASE, params=PARAMS,
                                      headers={"Authorization": f"Token {key}",
                                               "Content-Type": _mime_for(path)},
                                      data=f, timeout=timeout)
                if r.status_code == 200:
                    text, dur = _parse_response(r.json())
                    return {"status": "ok", "transcript": text, "duration_sec": dur,
                            "cost_usd": round(dur / 60 * PRICE_PER_MIN, 4), "key_fp": fp}
                if r.status_code in (401, 402, 403):
                    last_err = f"{r.status_code}: {r.text[:120]}"
                    mark_exhausted(key, state_path, status=f"exhausted ({r.status_code})")
                    log(f"[deepgram] ключ {fp} исчерпан ({last_err}) — следующий")
                    break
                if r.status_code == 429:
                    time.sleep(20 * (attempt + 1))
                    continue
                last_err = f"{r.status_code}: {r.text[:120]}"
                time.sleep(3 * (attempt + 1))
            except requests.RequestException as e:
                last_err = f"{type(e).__name__}: {str(e)[:120]}"
                time.sleep(5 * (attempt + 1))
    dead_final = exhausted_fps(state_path)
    if all(key_fp(k) in dead_final for k in keys):
        return {"status": "no_deepgram_credits", "transcript": "", "duration_sec": 0,
                "cost_usd": 0, "key_fp": "", "error": last_err}
    return {"status": "error", "transcript": "", "duration_sec": 0, "cost_usd": 0,
            "key_fp": "", "error": last_err}
