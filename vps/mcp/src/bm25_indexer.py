"""memory_bm25: mirror (main vault) → BM25 sparse-векторы → Qdrant vault_bm25.

Одна точка = одна заметка (id = uuid5(path)), payload: path, title, date, folder, channel.
Не индексирует: .obsidian, Trash, служебные Inbox/*/Search|Status|Trash, _index*.
Цикл раз в INTERVAL с (по умолчанию 300): изменившиеся по mtime+size → upsert, удалённые → delete.
avgdl фиксируется при первом полном проходе (state). Пересборка с нуля: удалить state + коллекцию.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .bm25 import doc_text, doc_vector

VAULT = Path(os.getenv("VAULT_PATH", "/vault"))
STATE = Path(os.getenv("BM25_STATE", "/state/bm25_state.json"))
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
COLL = os.getenv("QDRANT_BM25_COLLECTION", "vault_bm25")
INTERVAL = int(os.getenv("BM25_INTERVAL", "300"))
SKIP_DIRS = {".obsidian", ".trash", "Trash", ".git"}
DERIVED = re.compile(r"^Inbox/[^/]+/(Search|Status|Trash)/")


def _key():
    if os.getenv("QDRANT_API_KEY"):
        return os.environ["QDRANT_API_KEY"]
    try:
        return Path(os.getenv("QDRANT_KEY_FILE", "/qdrant-key")).read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


def _q(method, path, body=None):
    req = urllib.request.Request(QDRANT_URL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"api-key": _key(), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def ensure_collection():
    try:
        _q("GET", f"/collections/{COLL}")
        return
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    _q("PUT", f"/collections/{COLL}", {"vectors": {},
                                       "sparse_vectors": {"bm25": {"modifier": "idf"}}})
    for f in ("path", "folder", "channel", "date"):
        _q("PUT", f"/collections/{COLL}/index", {"field_name": f, "field_schema": "keyword"})
    print(f"[bm25] создана коллекция {COLL}", flush=True)


def fm_parse(text):
    props, body = {}, text
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            mm = re.match(r"^([\w-]+):\s*(.*)$", line)
            if mm:
                props[mm.group(1)] = mm.group(2).strip().strip('"')
        body = text[m.end():]
    return props, body


def scan():
    cur = {}
    for p in VAULT.rglob("*.md"):
        rel = p.relative_to(VAULT).as_posix()
        if any(part in SKIP_DIRS for part in p.parts) or DERIVED.match(rel) or p.name.startswith("_index"):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        cur[rel] = [st.st_mtime_ns, st.st_size]
    return cur


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"files": {}, "avgdl": 0}


def save_state(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE)


def point(rel, avgdl):
    raw = (VAULT / rel).read_text(encoding="utf-8", errors="replace")
    props, body = fm_parse(raw)
    stem = rel.rsplit("/", 1)[-1][:-3]
    vec, dl = doc_vector(doc_text(stem, body), avgdl)
    return {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"memsys-bm25/{rel}")),
            "vector": {"bm25": vec},
            "payload": {"path": rel, "title": stem, "date": props.get("created", "")[:10],
                        "folder": props.get("folder", ""), "channel": props.get("channel", "")}}, dl


def one_pass(state):
    cur = scan()
    files = state["files"]
    if not state.get("avgdl"):
        tot = n = 0
        for rel in cur:
            try:
                _, dl = point(rel, 560.0)
                tot += dl
                n += 1
            except OSError:
                pass
        state["avgdl"] = round(tot / max(n, 1), 1)
        print(f"[bm25] avgdl={state['avgdl']} по {n} заметкам", flush=True)
    changed = [r for r, v in cur.items() if files.get(r) != v]
    deleted = [r for r in files if r not in cur]
    if deleted:
        for i in range(0, len(deleted), 100):
            _q("POST", f"/collections/{COLL}/points/delete?wait=true",
               {"filter": {"must": [{"key": "path", "match": {"any": deleted[i:i + 100]}}]}})
        for r in deleted:
            files.pop(r, None)
    batch = []
    for k, rel in enumerate(changed, 1):
        try:
            pt, _ = point(rel, state["avgdl"])
        except OSError:
            continue
        batch.append((rel, pt))
        if len(batch) >= 100 or k == len(changed):
            _q("PUT", f"/collections/{COLL}/points?wait=true", {"points": [p for _, p in batch]})
            for r, _ in batch:
                files[r] = cur[r]
            save_state(state)
            batch = []
    if batch:
        _q("PUT", f"/collections/{COLL}/points?wait=true", {"points": [p for _, p in batch]})
        for r, _ in batch:
            files[r] = cur[r]
    save_state(state)
    print(f"[bm25] заметок {len(cur)}, обновлено {len(changed)}, удалено {len(deleted)}", flush=True)


def main():
    while True:
        try:
            ensure_collection()
            break
        except Exception as e:  # noqa: BLE001
            print(f"[bm25] жду Qdrant: {type(e).__name__}: {str(e)[:100]}", flush=True)
            time.sleep(20)
    while True:
        t = time.time()
        try:
            st = load_state()
            one_pass(st)
        except Exception as e:  # noqa: BLE001
            print(f"[bm25] ошибка прохода: {type(e).__name__}: {str(e)[:150]}", flush=True)
        print(f"[bm25] проход {time.time() - t:.0f} с, сплю {INTERVAL} с", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
