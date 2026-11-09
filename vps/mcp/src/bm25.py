"""BM25 (словесный поиск) для Memory System: общий токенизатор/стеммер и векторы.

Используется индексатором memory_bm25 (src/bm25_indexer.py) и сервером (src/server.py).
Заметка целиком (первые 20 000 символов тела, без frontmatter) = одна sparse-точка в Qdrant
коллекции QDRANT_BM25_COLLECTION (по умолчанию vault_bm25), sparse-вектор "bm25" с modifier=idf:
Qdrant сам считает IDF, мы кладём насыщенный TF (BM25 k1/b), запрос = термы со значением 1.
Стемминг: snowball russian/english (как в замере 07.11.2026, nDCG +0.09 к смысловому поиску).
"""
from __future__ import annotations

import re
import zlib

import snowballstemmer

_RU = snowballstemmer.stemmer("russian")
_EN = snowballstemmer.stemmer("english")
_TOK = re.compile(r"[a-zа-яё0-9]+")
_CYR = re.compile(r"[а-яё]")
_CACHE: dict[str, str] = {}
K1, B = 1.2, 0.75
BODY_CHARS = 20000


def _stem(w: str) -> str:
    s = _CACHE.get(w)
    if s is None:
        s = _RU.stemWord(w) if _CYR.search(w) else _EN.stemWord(w)
        if len(_CACHE) < 500000:
            _CACHE[w] = s
    return s


def tokens(text: str) -> list[str]:
    return [_stem(w) for w in _TOK.findall(text.lower().replace("ё", "е")) if len(w) > 1]


def term_id(t: str) -> int:
    return zlib.crc32(t.encode("utf-8")) & 0x7FFFFFFF


def doc_text(stem: str, body: str) -> str:
    return stem.replace("-", " ") + " " + body[:BODY_CHARS]


def doc_vector(text: str, avgdl: float) -> tuple[dict[str, list], int]:
    tf: dict[int, int] = {}
    toks = tokens(text)
    for t in toks:
        i = term_id(t)
        tf[i] = tf.get(i, 0) + 1
    dl = len(toks) or 1
    norm = K1 * (1 - B + B * dl / max(avgdl, 1.0))
    idx = sorted(tf)
    vals = [round(tf[i] * (K1 + 1) / (tf[i] + norm), 4) for i in idx]
    return {"indices": idx, "values": vals}, dl


def query_vector(text: str) -> dict[str, list]:
    idx = sorted({term_id(t) for t in tokens(text)})
    return {"indices": idx, "values": [1.0] * len(idx)}
