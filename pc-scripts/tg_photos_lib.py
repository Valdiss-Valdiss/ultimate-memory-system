"""Фото-вложения → mimo-v2.5 vision (зен/go/v1): классификация + дословный текст.

Патчим только осмысленные изображения (скриншоты, инфографика, таблицы, схемы,
слайды, документы). Мемы/фотографии без ценной текстовой нагрузки — терминальный
skip, чтобы не замусоривать семантический индекс.
"""
import base64
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tg_docs_lib import llm_chat

MEANINGFUL_TYPES = {"скриншот", "инфографика", "таблица", "схема", "диаграмма",
                    "документ", "слайд", "презентация", "чарт", "график"}

PROMPT = (
    "Опиши изображение строго по схеме:\n"
    "ТИП: один из [скриншот, инфографика, таблица, схема, диаграмма, документ, "
    "слайд, фото, мем, рисунок, другое]\n"
    "ОПИСАНИЕ: 1-2 предложения по-русски о сути изображения\n"
    "ТЕКСТ: весь текст с изображения дословно, построчно (если текста нет — ровно «текста нет»)\n"
    "Если на изображении таблица — переложи её в markdown. Без вступлений и markdown-заголовков."
)


def _field(txt, name):
    m = re.search(r"(?im)^" + name + r"\s*:\s*(.*)$", txt)
    return m.group(1).strip() if m else ""


def _field_block(txt, name):
    m = re.search(r"(?ims)^" + name + r"\s*:\s*(.*?)(?=^\s*(ТИП|ОПИСАНИЕ|ТЕКСТ)\s*:|\Z)", txt)
    return m.group(1).strip() if m else ""


def analyze_photo(path, mime="image/jpeg"):
    """Возвращает ({type, description, text, meaningful}, None) или (None, err)."""
    b64 = base64.b64encode(Path(path).read_bytes()).decode()
    parts = [{"type": "text", "text": PROMPT},
             {"type": "image_url", "image_url": {"url": f"data:{mime or 'image/jpeg'};base64,{b64}"}}]
    txt, err = llm_chat(parts, max_tokens=2500)
    if err:
        return None, err
    typ = _field(txt, "ТИП").lower().strip(" .")
    desc = _field(txt, "ОПИСАНИЕ")
    text = _field_block(txt, "ТЕКСТ")
    if not typ and not desc and not text:
        typ, desc, text = "другое", txt[:300], ""
    has_text = bool(text) and "текста нет" not in text.lower()
    meaningful = (typ in MEANINGFUL_TYPES) or (has_text and len(text) >= 150)
    return {"type": typ or "другое", "description": desc,
            "text": text if has_text else "", "meaningful": meaningful}, None
