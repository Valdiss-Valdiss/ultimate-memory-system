"""memory_indexer: mirror (main vault) → TEI (e5-large) → Qdrant коллекция vault_main_v2 (с 2026-11-08; раньше vault_main).

- Читает РАСШИФРОВАННЫЙ mirror (/srv/memory-system/vault/memory-ai) — CouchDB напрямую
  читать нельзя (LiveSync E2E-шифрование).
- Правила: только main vault (mount только его), sensitive недоступен физически;
  Qdrant — производный индекс, пересобирается с нуля за минуты.
- Цикл: скан *.md → изменившиеся (mtime+size) → чанки → "passage:"-эмбеддинги →
  удаление старых точек пути → upsert; удалённые файлы → удаление точек по path.
  Sensitive-папок в mirror нет.
- Лимиты: CHUNK_CHARS=1300 (укладывается в 512 токенов e5, auto_truncate);
  файлы > MAX_FILE (4 МБ): индексируется «голова» (HEAD_LIMIT) с пометкой в логе.
- State: index_state.json {rel_path: [mtime_ns, size]}. Интервал 300 с.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

VAULT = Path(os.environ.get("VAULT_DIR", "/mirror"))
STATE_DIR = Path(os.environ.get("INDEX_STATE_DIR", "/app/indexstate"))
TEI_URL = os.environ.get("TEI_URL", "http://memory_tei:80")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
COLL = os.environ.get("QDRANT_COLLECTION", "vault_main_v2")  # v2 с 2026-11-08; откат: vault_main + index_state.json
INTERVAL = int(os.environ.get("INDEX_INTERVAL", "300"))
EMB_DIM = 1024
CHUNK_CHARS = 1300  # ~512 токенов e5 для кириллицы (max_input_length модели)
SKIP_DIRS = (".obsidian", "Trash", ".trash", ".git")
MAX_FILE = 4_000_000    # до 4 МБ — индексируем целиком
HEAD_LIMIT = 1_500_000  # больше 4 МБ — индексируем «голову» (1.5 МБ) с пометкой в логе
E5_PASSAGE = "passage: "
E5_QUERY = "query: "

state_path = STATE_DIR / ("index_state.json" if COLL == "vault_main" else f"index_state_{COLL}.json")


def load_state():
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(s):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    tmp.replace(state_path)


def http_json(url, body=None, headers=None, method=None, timeout=120):
    req = urllib.request.Request(url,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers or {"Content-Type": "application/json"},
                                 method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def qdrant_headers():
    # ключ Qdrant: переменная окружения QDRANT_API_KEY (env_file .env) или /s.env (прод автора)
    key = os.environ.get("QDRANT_API_KEY", "")
    if not key and os.path.exists("/s.env"):
        for line in open("/s.env", encoding="utf-8-sig"):
            if line.startswith("QDRANT__SERVICE__API_KEY="):
                key = line.split("=", 1)[1].strip()
    return {"api-key": key, "Content-Type": "application/json"}


QH = None


def embed(texts):
    """Список текстов → список векторов (чанками по 16)."""
    out = []
    for i in range(0, len(texts), 16):
        part = texts[i:i + 16]
        r = http_json(f"{TEI_URL}/embed", {"inputs": part})
        if len(r) != len(part):
            raise RuntimeError(f"TEI вернул {len(r)} из {len(part)}")
        out.extend(r)
    return out


def fm_parse(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    props = {}
    body = text
    if m:
        for line in m.group(1).splitlines():
            mm = re.match(r"^([\w-]+):\s*(.*)$", line)
            if mm:
                props[mm.group(1)] = mm.group(2).strip().strip('"')
        body = text[m.end():]
    return props, body


def chunk_body(body, limit=CHUNK_CHARS):
    body = body.strip()
    if len(body) <= limit:
        return [body] if body else []
    parts, cur = [], ""
    for para in re.split(r"\n\s*\n", body):
        if len(cur) + len(para) + 2 <= limit:
            cur = f"{cur}\n\n{para}".strip()
        else:
            if cur:
                parts.append(cur)
                cur = ""
            if len(para) <= limit:
                cur = para
            else:
                # абзац длиннее лимита — режем на части, ничего не теряем
                for k in range(0, len(para), limit):
                    parts.append(para[k:k + limit])
    if cur:
        parts.append(cur)
    return parts


def make_passages(rel_path, props, body, stem):
    # Формат v2 (2026-11-08, решение пользователя): чистый текст без служебных полей.
    # Кусок 0: имя + канал + папка; куски 1+: имя + короткое описание заметки (контекст).
    # Совпадает с /tmp/reemb/export.py, по которому посчитана коллекция vault_main_v2.
    chunks = chunk_body(body)
    if not chunks:
        chunks = [f"(заметка без текста) {stem}"]
    head = f"{stem}. Канал: {props.get('channel', '')}. Папка: {props.get('folder', '')}."
    ctx = _fn_desc(rel_path, 220) if len(chunks) > 1 else ""
    passages = []
    for idx, ch in enumerate(chunks):
        pre = head if idx == 0 else f"{stem}. {ctx}"
        passages.append(E5_PASSAGE + f"{pre} {ch}"[:6000])
    return passages


# --- описание заметки: копия _fn_* из mcp/src/server.py (менять синхронно!) ---
import re as _re_fn

_FN_MDLINK = _re_fn.compile(r"\[([^\]]*)\]\([^)]*\)")
_FN_SUMMARY = [_re_fn.compile(r"^- \*\*Резюме:\*\*\s*(.+)$", _re_fn.M),
               _re_fn.compile(r"^- \*\*Резюме документа:\*\*\s*(.+)$", _re_fn.M),
               _re_fn.compile(r"^- \*\*Описание:\*\*\s*(.+)$", _re_fn.M)]
_FN_SKIP = ("- **", "---", "📎", "🔗", "![", "|", "```", "> ")


_FN_EMOJI = _re_fn.compile("[\U0001F000-\U0001FFFF\u2600-\u27BF\u2190-\u21FF\u2B00-\u2BFF\uFE0F\u200D\u20E3]")


def _fn_clean(s: str) -> str:
    s = _FN_MDLINK.sub(r"\1", s)
    s = _re_fn.sub(r"https?://\S+", "", s)
    s = _re_fn.sub(r"#[\w-]+", "", s)
    s = _FN_EMOJI.sub("", s).replace("\u200b", "")
    s = _re_fn.sub(r"[*_`>#]+", "", s)
    s = _re_fn.sub(r"\s+", " ", s)
    return s.strip(" -—:·|•")


def _fn_cut(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    c = s[:n]
    i = c.rfind(" ")
    return (c[:i] if i > n * 0.6 else c).rstrip(" ,.;:—-") + "…"


def _fn_field(body: str, rx) -> str:
    m = rx.search(body)
    if not m:
        return ""
    v = _fn_clean(m.group(1))
    if not v or v.strip("_() ").lower() in ("нет", "") or _re_fn.fullmatch(r"\[.*\]", v):
        return ""
    return v


def _fn_digest(rel: str) -> dict:
    """Название и короткое резюме заметки — из её собственных полей: резюме видео/аудио и
    документов (их сделала LLM конвейера при импорте), текст поста, описание картинки.
    Ничего не генерирует."""
    try:
        text = (VAULT / rel).read_text(encoding="utf-8", errors="replace")[:30000]
    except Exception:  # noqa: BLE001
        text = ""
    body = text
    if body.startswith("---"):
        end = body.find("\n---", 3)
        body = body[end + 4:] if end > 0 else body
    media_sum = _fn_field(body, _FN_SUMMARY[0]) or _fn_field(body, _FN_SUMMARY[1])
    img_sum = _fn_field(body, _FN_SUMMARY[2])
    title = ""
    m = _re_fn.search(r"^## 📺 (.+)$", body, _re_fn.M)
    if m:
        title = _fn_clean(m.group(1))
    rest = []
    for line in body.splitlines():
        ls = line.strip().replace("\u200b", "")
        if ls == "---":
            break  # дальше — разделы конвейера (OCR/транскрипты), это не текст поста
        if ls.startswith("#"):
            h = ls.lstrip("#").strip()
            if _FN_EMOJI.search(h) or _re_fn.match(r"(Транскрипт|Описание|Текст документа|Резюме|OCR|Медиа)", h):
                break  # служебный раздел конвейера
            ls = h  # заголовок самого поста — это текст
        if not ls or ls.startswith(_FN_SKIP):
            continue
        c = _fn_clean(ls)
        if len(c) < 8 or c.lower().startswith(("медиа:", "автор", "источник")):
            continue
        if not title:
            parts = _re_fn.split(r"(?<=[.!?])\s+", c, maxsplit=1)
            title = parts[0]
            if len(parts) > 1 and len(parts[1]) >= 8:
                rest.append(parts[1])
        elif c != title:
            rest.append(c)
        if len(" ".join(rest)) > 400:
            break
    if not title:
        stem = rel.rsplit("/", 1)[-1]
        stem = stem[:-3] if stem.endswith(".md") else stem
        title = _re_fn.sub(r"^\d{4}-\d{2}-\d{2}-", "", stem).replace("-", " ")
    title = title.rstrip(" .")
    post = " ".join(rest)
    summary = media_sum or (post if len(post) >= 40 else "") or img_sum or post
    if summary.lower().startswith(title.lower()[:40]):
        summary = summary[len(title):].strip(" .—-:")
    return {"title": _fn_cut(title, 140), "summary": _fn_cut(summary, 400)}


def _fn_desc(rel: str, n: int = 300) -> str:
    """Описание заметки в 2–4 строки — из её собственных полей (резюме конвейера / текст поста)."""
    d = _fn_digest(rel)
    stem = rel.rsplit("/", 1)[-1][:-3]
    title, summ = d["title"].rstrip("…"), d["summary"]
    stem_words = _re_fn.sub(r"^\d{4}-\d{2}-\d{2}-", "", stem).replace("-", " ").lower()
    parts = []
    if title and title.lower() != stem_words:
        parts.append(title.rstrip(" ."))
    if summ:
        parts.append(summ)
    if len(parts) == 2 and parts[0][-1:] in "?!…:":
        desc = parts[0] + " " + parts[1]
    else:
        desc = ". ".join(parts).strip()
    return _fn_cut(desc, n) if desc else "(в заметке нет текста — только медиа или ссылка)"


def note_pass(rel_path, props, body, stem):
    """Пассаж для заметки целиком (1 чанк)."""
    meta = " ".join(f"{k}: {v}" for k, v in sorted(props.items()) if v)[:400]
    return E5_PASSAGE + f"{stem}. {meta}. {body}"[:6000]


def delete_paths(paths):
    selector = {"must": [{"key": "path", "match": {"any": list(paths)}}]}
    http_json(f"{QDRANT_URL}/collections/{COLL}/points/delete",
              {"filter": selector}, headers=QH, method="POST")


def index_pass(qh):
    state = load_state()
    current = {}
    changed = []
    for p in VAULT.rglob("*.md"):
        rel = p.relative_to(VAULT).as_posix()
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.name == "_index.md":
            continue
        if re.match(r"^Inbox/[^/]+/(Search|Status|Trash)/", rel):
            continue  # служебные заметки поиска/статуса — не индексируем (2026-11-07)
        try:
            st = p.stat()
        except OSError:
            continue
        current[rel] = [st.st_mtime_ns, st.st_size]
        prev = state.get(rel)
        if prev is None or prev[0] != st.st_mtime_ns or prev[1] != st.st_size:
            changed.append((rel, p, st))
    deleted = [k for k in state if k not in current]
    print(f"[index] файлов: {len(current)}, изменилось: {len(changed)}, "
          f"удалено: {len(deleted)}", flush=True)

    if deleted:
        for i in range(0, len(deleted), 50):
            delete_paths(deleted[i:i + 50])
        for k in deleted:
            state.pop(k, None)
        save_state(state)  # сразу фиксируем вычистку (иначе хвост state ждёт следующего батча)
        print(f"[index] удалено точек по {len(deleted)} путям", flush=True)

    for batch_start in range(0, len(changed), 20):
        if stop_flag.is_set():
            break
        batch = changed[batch_start:batch_start + 20]
        points = []
        texts = []
        metas = []
        processed_rels = []
        for rel, p, st in batch:
            try:
                raw = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if st.st_size > MAX_FILE:
                raw = raw[:HEAD_LIMIT]
                print(f"[index] HEAD >{MAX_FILE / 1e6:.0f}MB, первые "
                      f"{HEAD_LIMIT / 1e6:.1f}MB: {rel}", flush=True)
            props, body = fm_parse(raw)
            processed_rels.append(rel)
            stem = p.stem
            passages = make_passages(rel, props, body, stem)
            for idx, pas in enumerate(passages):
                texts.append(pas)
                metas.append({
                    "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"memsys/{rel}#{idx}")),
                    "payload": {
                        "path": rel,
                        "folder": props.get("folder", ""),
                        "channel": props.get("channel", ""),
                        "date": props.get("created", "")[:10],
                        "type": props.get("type", ""),
                        "status": props.get("status", ""),
                        "msg_id": props.get("msg_id", ""),
                        "title": stem,
                        "chunk": idx,
                        "n_chunks": len(passages),
                        "snippet": body[:500],
                    },
                })
        if not texts:
            continue
        vectors = embed(texts)
        for mt, vec in zip(metas, vectors):
            points.append({"id": mt["id"], "vector": vec, "payload": mt["payload"]})
        # Постоянный фикс хвостов: удаляем ВСЕ прежние точки путей из батча
        # (заметка сократилась → чанков меньше → старые хвостовые исчезают)
        if processed_rels:
            delete_paths(processed_rels)
        for i in range(0, len(points), 64):
            http_json(f"{QDRANT_URL}/collections/{COLL}/points",
                      {"points": points[i:i + 64]}, headers=qh, method="PUT")
        for rel, p, st in batch:
            state[rel] = [st.st_mtime_ns, st.st_size]
        save_state(state)
        print(f"[index] +{len(batch)} заметок (точек {len(points)}), "
              f"итого {batch_start + len(batch)}/{len(changed)}", flush=True)


stop_flag = type("X", (), {"is_set": lambda self: False})()  # заглушка под graceful stop


def main():
    global QH
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    QH = qdrant_headers()
    # Ждём готовности TEI
    while True:
        try:
            r = http_json(f"{TEI_URL}/embed", {"inputs": [E5_PASSAGE + "warmup"]}, timeout=30)
            if r and len(r[0]) == EMB_DIM:
                print("[index] TEI готов", flush=True)
                break
        except Exception as e:
            print(f"[index] жду TEI: {type(e).__name__}", flush=True)
            time.sleep(20)
    while True:
        try:
            t0 = time.time()
            index_pass(QH)
            print(f"[index] цикл завершён за {time.time() - t0:.0f}с, сплю {INTERVAL}с",
                  flush=True)
        except Exception as e:
            print(f"[index] ошибка цикла: {type(e).__name__}: {str(e)[:120]}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
