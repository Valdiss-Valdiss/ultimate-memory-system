"""Одноразовый конвертер бэкфилла: каталог с sidecar.json + файл → JSON-результат в stdout.

Запуск (ПК): scp файла+sidecar в /tmp/docbf/<ключ>/, затем
  docker run --rm -v /tmp/docbf/<ключ>:/w memory-tg-tools:latest python /app/convert_one.py /w
Реиспользует tg_docs_lib/tg_photos_lib — та же логика, что у живого обогатителя.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "/app")
import tg_docs_lib as doclib
import tg_photos_lib as photolib


def main():
    workdir = Path(sys.argv[1] if len(sys.argv) > 1 else "/w")
    sc = json.loads((workdir / "sidecar.json").read_text(encoding="utf-8"))
    files = [p for p in workdir.iterdir() if p.name != "sidecar.json"]
    if not files:
        print(json.dumps({"status": "error", "error": "no file"}, ensure_ascii=False))
        return
    f = files[0]
    job_key = f"{sc.get('channel_id')}:{sc.get('msg_id')}:{sc.get('title') or f.name}"
    if sc.get("kind") == "photo":
        res, err = photolib.analyze_photo(f, sc.get("mime") or "image/jpeg")
        if err:
            print(json.dumps({"status": "error", "error": str(err)[:200]}, ensure_ascii=False))
        else:
            print(json.dumps({"status": "ok", "kind": "photo", **res}, ensure_ascii=False))
        return
    st, text = doclib.convert_document(f, sc.get("mime") or "", job_key=job_key)
    if st == "wait_job":
        print(json.dumps({"status": "wait_job"}, ensure_ascii=False))
        return
    if st == "empty" and ((sc.get("mime") or "").endswith("pdf") or f.suffix.lower() == ".pdf"):
        st, text = doclib.mimo_scan_text(f, sc.get("mime") or "application/pdf")
    if st == "ok":
        summary = doclib.doc_summary(text) or ""
        out = {"status": "ok", "kind": "doc", "summary": summary,
               "md": doclib.cap_text(text)}
        if not summary:
            out["summary_error"] = (getattr(doclib, "LAST_LLM_ERR", "") or "empty after retries")[:160]
        print(json.dumps(out, ensure_ascii=False))
    elif st == "unsupported":
        print(json.dumps({"status": "unsupported", "ext": str(text)}, ensure_ascii=False))
    else:
        print(json.dumps({"status": "error", "error": str(text)[:200]}, ensure_ascii=False))


main()
