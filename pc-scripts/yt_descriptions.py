"""Описания + топ-комментарии (YouTube Data API) → LLM-резюме-предположение для безсубтитровых заметок.

python yt_descriptions.py [--limit N]
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import os

from tg_common import SCRIPT_DIR
from yt_enrich import OUT, llm_key, llm_candidates, llm_call, LLM_MODEL


def yt_key():
    env_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if env_key:
        return env_key
    env_path = SCRIPT_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and line.split("=", 1)[0].strip() == "YOUTUBE_API_KEY":
                k = line.split("=", 1)[1].strip()
                if k:
                    return k
    return None


def yt_api(path, params):
    url = f"https://www.googleapis.com/youtube/v3/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        print(f"    YT API HTTP {e.code}: {body}", flush=True)
        return None


def yt_descriptions_batch(ids):
    out = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        d = yt_api("videos", {"part": "snippet", "id": ",".join(chunk), "key": yt_key()})
        for item in (d or {}).get("items", []):
            out[item["id"]] = (item.get("snippet") or {}).get("description", "")
        print(f"    videos.list: {min(i + 50, len(ids))}/{len(ids)}", flush=True)
    return out


def yt_comments(vid, max_results=15):
    d = yt_api("commentThreads", {"part": "snippet", "videoId": vid,
                                  "maxResults": max_results, "order": "relevance",
                                  "textFormat": "plainText", "key": yt_key()})
    out = []
    for item in (d or {}).get("items", []):
        top = (item.get("snippet") or {}).get("topLevelComment") or {}
        text = ((top.get("snippet") or {}).get("textDisplay") or "").strip()
        likes = (top.get("snippet") or {}).get("likeCount", 0)
        if text:
            out.append((likes, text))
    return [t for _, t in sorted(out, reverse=True)]


def llm_guess_summary(title, description, comments):
    if not llm_candidates():
        return ""
    parts = [f"Название видео: {title}"]
    if description:
        parts.append(f"Описание:\n{description[:3000]}")
    if comments:
        parts.append("Популярные комментарии:\n" + "\n".join("- " + c[:300] for c in comments[:10]))
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "Ты предполагаешь, о чём YouTube-видео, по его названию, описанию и комментариям (транскрипта нет). 2-4 предложения на русском: вероятная тема, о чём говорит автор, чем может быть полезно зрителю. Без вступлений."},
            {"role": "user", "content": "\n\n".join(parts)},
        ],
        "max_tokens": 1200,
    }).encode()
    d, err = llm_call(body)
    if d is None:
        print(f"    LLM guess error: {str(err)[:110]}", flush=True)
        return ""
    msg = d["choices"][0]["message"]
    txt = (msg.get("content") or msg.get("reasoning_content") or "").strip()
    return re.sub(r"\s+", " ", txt)


WARNING = "- **Резюме:** ⚠️ транскрипт недоступен (нет субтитров у видео — даже автоматических)"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    if not yt_key():
        sys.exit("[!] Нет YOUTUBE_API_KEY в .env")
    if not llm_key():
        sys.exit("[!] Нет LLM-ключа")
    targets = []
    for p in OUT.rglob("*.md"):
        if p.name in ("_index.md", "report.md"):
            continue
        content = p.read_text(encoding="utf-8")
        if "## Описание" in content or "youtube:" not in content:
            continue
        m = re.search(r"^youtube: https://youtu\.be/([\w\-]+)$", content, re.M)
        if m and "транскрипт недоступен" in content:
            targets.append((p, m.group(1)))
    if args.limit:
        targets = targets[:args.limit]
    print(f"Безсубтитровых заметок к обработке: {len(targets)}")
    if not targets:
        return
    ids = sorted({vid for _, vid in targets})
    descs = yt_descriptions_batch(ids)
    ok = 0
    for n, (path, vid) in enumerate(targets, 1):
        content = path.read_text(encoding="utf-8")
        desc = descs.get(vid)
        tm = re.search(r"## 📺 (.+)", content)
        title = tm.group(1) if tm else vid
        if desc is None:
            print(f"  [{n}] {vid}: видео недоступно — помечаю", flush=True)
            content = content.replace(
                WARNING,
                "- **Резюме:** ⚠️ транскрипта нет; видео недоступно или удалено\n\n## Описание\n\n⚠️ Видео недоступно на YouTube.")
            path.write_text(content, encoding="utf-8")
            continue
        comments = yt_comments(vid)
        desc_r = desc.strip()
        if desc_r and not re.search(r"[а-яА-ЯёЁ]", desc_r):
            desc_r = llm_translate(desc_r)
        guess = llm_guess_summary(title, desc, comments)
        section = "\n## Описание\n\n" + (desc_r or "_(описания нет)_")
        if guess:
            section += "\n\n**Резюме-предположение (по описанию и комментариям):** " + guess
        else:
            section += "\n\n_(резюме-предположение не сформировано)_"
        content = content.replace(WARNING, "- **Резюме:** ⚠️ транскрипта нет (у видео нет субтитров)" + section)
        path.write_text(content, encoding="utf-8")
        ok += 1
        print(f"  [{n}] {vid}: описано ({len(desc)} симв. описания, {len(comments)} комм.)", flush=True)
    print(f"\nИтог: обработано {ok}/{len(targets)}")


if __name__ == "__main__":
    main()
