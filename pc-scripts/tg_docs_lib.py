"""Конвертация документов в markdown: docling-serve (VPS) + mimo file-part (сканы) + FB2.

Ветка «книжный конвейер» (2026-09). Docling-контейнер — общий для стека:
не рестартим, не конфигурируем — только HTTP. OCR в docling всегда выключен
(сканы уходят в mimo-vision): авто-OCR там выбирает EasyOCR (RAM-пик 11+ ГБ,
известная утечка в контейнерах) — поэтому do_ocr=false на каждом запросе.
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yt_enrich as ye  # ZEN_GO, LLM_MODEL, LLM_SESSION, llm_key()

DOC_SERVE = os.environ.get("DOC_SERVE_URL", "http://docling:5001")
DOC_TIMEOUT = int(os.environ.get("DOC_TIMEOUT", "2400"))
SCAN_TEXT_MIN = 200          # меньше знаков после конвертации → считаем сканом
DOC_TEXT_CAP = 500_000       # кап текста документа в заметке
MIMO_SCAN_FILE_MAX = 100 * 1024 * 1024   # крупнее скан — сразу рендер страниц
SCAN_PAGE_CAP = 30           # сколько первых страниц скана отправлять в mimo
SCAN_PAGES_PER_CALL = 8      # страниц за один mimo-вызов

SUPPORTED_EXT = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".epub": "application/epub+zip",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".html": "text/html", ".htm": "text/html", ".xhtml": "text/html",
    ".csv": "text/csv", ".tsv": "text/csv",
    ".md": "text/markdown", ".txt": "text/plain", ".text": "text/plain",
    ".json": "application/json", ".xml": "application/xml",
    ".adoc": "text/asciidoc", ".asciidoc": "text/asciidoc",
    ".tex": "text/x-tex", ".latex": "text/x-latex",
    ".eml": "message/rfc822", ".msg": "application/vnd.ms-outlook",
}


def cap_text(text, limit=DOC_TEXT_CAP):
    t = text.strip()
    if len(t) <= limit:
        return t
    return t[:limit].rstrip() + "\n\n> _Текст усечён — полный файл в посте Telegram._"


# --- LLM (mimo-v2.5 через zen/go/v1, как в yt_enrich) -----------------------

def llm_chat(parts, max_tokens=1500, system=None):
    """Возвращает (текст, None) или (None, ошибка)."""
    key = ye.llm_key()
    if not key:
        return None, "no LLM key"
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": parts})
    body = json.dumps({"model": ye.LLM_MODEL, "messages": msgs,
                       "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(ye.ZEN_GO + "/chat/completions", data=body, method="POST")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0")
    req.add_header("x-opencode-session", ye.LLM_SESSION)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            d = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode(errors='replace')[:120]}"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:120]}"
    m = d["choices"][0]["message"]
    txt = (m.get("content") or m.get("reasoning") or m.get("reasoning_content") or "").strip()
    txt = re.sub(r"^\s*<think>.*?</think>\s*", "", txt, flags=re.S)
    fr = d["choices"][0].get("finish_reason")
    if not txt:
        return None, f"empty response (finish={fr}, keys={[k for k in m.keys() if k in ('content', 'reasoning', 'reasoning_content') and m.get(k)]})"
    return txt, None


# --- docling ---------------------------------------------------------------

def _jobs_file():
    return Path(os.environ.get("DOC_JOBS_FILE", "/app/state/docling_jobs.json"))


def _jobs_load():
    try:
        return json.loads(_jobs_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _jobs_save(reg):
    try:
        p = _jobs_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def _poll_status(task):
    with urllib.request.urlopen(DOC_SERVE + f"/v1/status/poll/{task}", timeout=30) as r:
        return json.loads(r.read().decode())


def _fetch_md(task):
    try:
        with urllib.request.urlopen(DOC_SERVE + f"/v1/result/{task}", timeout=120) as r:
            d = json.loads(r.read().decode())
        return (d.get("document") or {}).get("md_content") or ""
    except Exception:
        return ""


def docling_convert(path, timeout=DOC_TIMEOUT, job_key=None):
    """docling-serve: файл → markdown. Возобновляемо: task_id живёт в реестре
    (state/docling_jobs.json), повторная встреча с «недоконченным» job'ом
    продолжает поллинг вместо повторного прогона. Возвращает
    (status, text|err): ok | empty | error | wait_job."""
    reg = _jobs_load()
    key = job_key or f"path:{path.name}:{path.stat().st_size}"
    task = (reg.get(key) or {}).get("task_id")
    st = None
    if task:
        try:
            st = _poll_status(task).get("task_status")
        except Exception:
            st = None
        if st == "success":
            md = _fetch_md(task)
            reg.pop(key, None)
            _jobs_save(reg)
            return ("ok", md) if md.strip() else ("empty", "")
        if st == "failure":
            reg.pop(key, None)
            _jobs_save(reg)
            task = None  # подаём заново
    b = uuid.uuid4().hex
    opt = {"do_ocr": "false", "include_images": "false", "image_export_mode": "placeholder",
           "abort_on_error": "false", "document_timeout": "3600"}
    chunks = []
    chunks.append(("--" + b + "\r\nContent-Disposition: form-data; name=\"files\"; "
                   "filename=\"document" + path.suffix.lower() + "\"\r\n"
                   "Content-Type: application/octet-stream\r\n\r\n").encode("utf-8"))
    chunks.append(path.read_bytes())
    tail = "\r\n"
    for name, val in opt.items():
        tail += ("--" + b + "\r\nContent-Disposition: form-data; name=\"" + name +
                 "\"\r\n\r\n" + val + "\r\n")
    tail += "--" + b + "--\r\n"
    chunks.append(tail.encode("utf-8"))
    req = urllib.request.Request(DOC_SERVE + "/v1/convert/file/async",
                                 data=b"".join(chunks), method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + b)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            task = json.loads(resp.read().decode())["task_id"]
        reg[key] = {"task_id": task, "ts": time.time()}
        _jobs_save(reg)
    except urllib.error.HTTPError as e:
        return "error", f"docling HTTP {e.code}: {e.read().decode(errors='replace')[:120]}"
    except Exception as e:
        return "error", f"docling {type(e).__name__}: {str(e)[:120]}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        try:
            st = _poll_status(task).get("task_status")
        except Exception:
            st = None
        if st in ("success", "failure"):
            break
    if st == "success":
        md = _fetch_md(task)
        reg.pop(key, None)
        _jobs_save(reg)
        return ("ok", md) if md.strip() else ("empty", "")
    if st == "failure":
        reg.pop(key, None)
        _jobs_save(reg)
        return "error", "docling job failure"
    return "wait_job", ""  # job продолжает работать на сервере — заберём при следующей встрече


# --- FB2 (docling не поддерживает; это XML) --------------------------------

_FB2 = "{http://www.gribuser.ru/xml/fictionbook/2.0}"


def _fb2_text(el):
    if el is None:
        return ""
    parts = []
    for t in el.itertext():
        parts.append(t or "")
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def fb2_to_md(path):
    try:
        root = ET.parse(str(path)).getroot()
    except Exception as e:
        return "error", f"fb2 {type(e).__name__}: {str(e)[:100]}"
    out = []
    ti = root.find(f"{_FB2}description/{_FB2}title-info")
    if ti is not None:
        title = ti.find(f"{_FB2}book-title")
        if title is not None and _fb2_text(title):
            out.append("# " + _fb2_text(title))
        author = ti.find(f"{_FB2}author")
        if author is not None:
            names = [_fb2_text(author.find(f"{_FB2}{k}")) for k in ("first-name", "middle-name", "last-name")]
            who = " ".join(n for n in names if n).strip()
            if who:
                out.append("*" + who + "*")
    def walk(el, depth):
        title = el.find(f"{_FB2}title")
        if title is not None and _fb2_text(title):
            out.append("\n" + "#" * min(max(depth, 2), 6) + " " + _fb2_text(title))
        for ch in list(el):
            tag = ch.tag
            if tag == title:
                continue
            if tag == _FB2 + "section":
                walk(ch, depth + 1)
            elif tag == _FB2 + "p":
                t = _fb2_text(ch)
                if t:
                    out.append(t)
            elif tag == _FB2 + "epigraph":
                t = _fb2_text(ch)
                if t:
                    out.append("> " + t)
            elif tag in (_FB2 + "poem", _FB2 + "cite"):
                t = _fb2_text(ch)
                if t:
                    out.append("> " + t)
    for body in root.findall(f"{_FB2}body"):
        walk(body, 1)
    md = "\n\n".join(out).strip()
    if not md:
        return "empty", ""
    return "ok", md


# --- диспетчер конвертации --------------------------------------------------

def pdf_text_probe(path, first_n=5):
    """Быстрый пробник текстового слоя (pypdfium2): (всего_страниц, текст) или (None, '')."""
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(path))
        total = len(pdf)
        t = []
        for i in range(min(first_n, total)):
            page = pdf[i]
            t.append(page.get_textpage().get_text_range() or "")
        return total, "\n".join(t)
    except Exception:
        return None, ""


def doc_summary(text):
    """Нейтральное резюме документа (mimo). Возвращает текст или ''.

    Ретраи ×2 с паузой, ошибки — в stderr (попадает в enrich.log / r.stderr
    и в лог бэкфилла через summary_error в JSON). Урок 2026-09-14: тихие сбои
    (таймаут при троттлинге VPS) дали 37% пустых резюме (40/109).
    """
LAST_LLM_ERR = ""


def doc_summary(text):
    """Нейтральное резюме документа (mimo). Возвращает текст или ''.

    Ретраи ×3 с нарастающей паузой (30/90 с), ошибки — в stderr (попадает в enrich.log
    / r.stderr и в лог бэкфилла через summary_error в JSON). Урок 2026-09-14: тихие
    сбои (таймаут при троттлинге VPS) дали 37% пустых резюме (40/109). Текст последней
    ошибки — в LAST_LLM_ERR (пробрасывается convert_one'ом в JSON → лог бэкфилла).
    """
    global LAST_LLM_ERR
    sysmsg = ("Ты кратко излагаешь содержание документа на русском языке: суть, ключевые "
              "факты, цифры и выводы. 2-5 предложений. Без вступлений, оценок и markdown. "
              "Если документ не для пересказа (инструкция/каталог) — опиши, что это и из чего состоит.")
    for attempt, pause in enumerate((0, 30, 90)):
        if pause:
            print(f"[llm] doc_summary ретрай через {pause} с", file=sys.stderr, flush=True)
            time.sleep(pause)
        txt, err = llm_chat(text[:60000], max_tokens=3000, system=sysmsg)
        if txt:
            LAST_LLM_ERR = ""
            return txt
        LAST_LLM_ERR = err or ""
        print(f"[llm] doc_summary попытка {attempt + 1}/3 ошибка: {err}",
              file=sys.stderr, flush=True)
    return ""


def pdf_full_text(path):
    """Полный текст PDF через pypdfium2 (лёгкий, секунды). None при сбое."""
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(path))
        t = []
        for i in range(len(pdf)):
            t.append(pdf[i].get_textpage().get_text_range() or "")
        return "\n".join(t)
    except Exception:
        return None


def convert_document(path, mime="", job_key=None):
    """(status, text|error): ok | empty | unsupported | error | wait_job.

    PDF-стратегия: полный текст пробником pypdfium2 (лёгкий, секунды):
      - нет текстового слоя → скан → mimo (минуя docling);
      - текст есть, страниц ≤ 40 → docling (структурный md);
      - текст есть, крупный (книга) → быстрый путь без docling
        (docling-parse на сотнях страниц превышает таймаут и греет CPU).
    """
    ext = path.suffix.lower()
    if ext == ".fb2":
        return fb2_to_md(path)
    known = ext in SUPPORTED_EXT or mime in SUPPORTED_EXT.values()
    if not known:
        return "unsupported", ext or mime or "unknown"
    if ext == ".pdf" or mime == "application/pdf":
        total_pages, probe = pdf_text_probe(path, first_n=5)
        if total_pages is None:
            return docling_convert(path, job_key=job_key)
        if looks_like_scan(probe):
            return mimo_scan_text(path, mime or "application/pdf")
        max_pages = int(os.environ.get("PDF_DOCLING_MAX_PAGES", "40"))
        if total_pages > max_pages:
            full = pdf_full_text(path)
            if full and not looks_like_scan(full):
                return "ok", full
            return mimo_scan_text(path, mime or "application/pdf")
    return docling_convert(path, job_key=job_key)


def looks_like_scan(text):
    return len(re.sub(r"\s+", "", text or "")) < SCAN_TEXT_MIN


def _render_pages(path, max_pages=SCAN_PAGE_CAP):
    try:
        import pypdfium2 as pdfium
    except Exception as e:
        return None, f"pypdfium2 {type(e).__name__}"
    try:
        pdf = pdfium.PdfDocument(str(path))
        total = len(pdf)
        pages = []
        for i in range(min(max_pages, total)):
            bitmap = pdf[i].render(scale=2.0)
            pil = bitmap.to_pil()
            buf = __import__("io").BytesIO()
            pil.save(buf, format="JPEG", quality=82)
            pages.append(buf.getvalue())
        return (pages, total), None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:100]}"


def mimo_scan_text(path, mime=""):
    """Графический PDF → mimo: сначала file-part, затем рендер страниц → vision."""
    size = path.stat().st_size
    if size <= MIMO_SCAN_FILE_MAX and (mime or "").endswith("pdf"):
        b64 = base64.b64encode(path.read_bytes()).decode()
        txt, err = llm_chat([
            {"type": "text", "text": "Это скан документа без текстового слоя. Извлеки весь текст "
                                     "со всех страниц дословно в markdown. Если текста нет — ровно «текста нет»."},
            {"type": "file", "file": {"filename": "scan.pdf", "file_data": f"data:application/pdf;base64,{b64}"}},
        ], max_tokens=8000)
        if txt and not looks_like_scan(txt):
            return "ok", txt
    pages_total, err = _render_pages(path)
    if pages_total is None:
        return "error", (err or "render failed")
    pages, total = pages_total
    combined = []
    for start in range(0, len(pages), SCAN_PAGES_PER_CALL):
        part = pages[start:start + SCAN_PAGES_PER_CALL]
        parts = [{"type": "text", "text": "Ниже страницы скана документа по порядку. Извлеки текст "
                  "каждой дословно, помечая «— Страница N —». Если на странице текста нет — так и напиши."}]
        for i, png in enumerate(part):
            b64 = base64.b64encode(png).decode()
            parts.append({"type": "text", "text": f"Страница {start + i + 1}:"})
            parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        txt, err = llm_chat(parts, max_tokens=6000)
        if txt and "текста нет" not in txt.lower():
            combined.append(txt)
    md = "\n\n".join(combined).strip()
    if not md or looks_like_scan(md):
        return "empty", ""
    if total > len(pages):
        md += f"\n\n> _Показаны первые {len(pages)} из {total} страниц._"
    return "ok", md
