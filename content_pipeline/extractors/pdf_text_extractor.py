from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from content_pipeline.processors.clean_text import clean_extracted_text, normalize_for_matching


@dataclass(frozen=True)
class PageText:
    page: int
    text: str
    char_count: int
    has_text: bool


def _import_fitz():
    try:
        import fitz  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "PyMuPDF no está instalado. Instala dependencias con: "
            "env/Scripts/pip.exe install -r requirements.txt"
        ) from exc
    return fitz


def _line_key(line: str) -> str:
    return normalize_for_matching(line)


def _remove_repeated_headers_footers(raw_pages: list[str]) -> list[str]:
    if len(raw_pages) < 4:
        return raw_pages

    by_key: dict[str, set[int]] = defaultdict(set)
    for index, text in enumerate(raw_pages):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        candidates = lines[:2] + lines[-2:]
        for line in candidates:
            key = _line_key(line)
            if key and len(key) < 90:
                by_key[key].add(index)

    threshold = max(3, len(raw_pages) // 3)
    repeated = {key for key, pages in by_key.items() if len(pages) >= threshold}
    if not repeated:
        return raw_pages

    cleaned_pages: list[str] = []
    for text in raw_pages:
        kept_lines = []
        for line in text.splitlines():
            key = _line_key(line)
            if key in repeated and len(key) < 90:
                continue
            kept_lines.append(line)
        cleaned_pages.append("\n".join(kept_lines))
    return cleaned_pages


def extract_pdf_pages(pdf_path: Path) -> list[dict[str, object]]:
    if not pdf_path.exists():
        raise FileNotFoundError(f"No existe el PDF de entrada: {pdf_path}")

    fitz = _import_fitz()
    raw_pages: list[str] = []
    with fitz.open(pdf_path) as document:
        for page in document:
            raw_pages.append(page.get_text("text") or "")

    raw_pages = _remove_repeated_headers_footers(raw_pages)
    pages: list[PageText] = []
    for index, raw_text in enumerate(raw_pages, start=1):
        text = clean_extracted_text(raw_text)
        pages.append(
            PageText(
                page=index,
                text=text,
                char_count=len(text),
                has_text=bool(text.strip()),
            )
        )
    return [asdict(page) for page in pages]
