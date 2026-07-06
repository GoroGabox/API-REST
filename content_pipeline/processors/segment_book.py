from __future__ import annotations

from dataclasses import asdict, dataclass

from content_pipeline.processors.clean_text import (
    extract_keywords,
    is_probable_heading,
    shorten_text,
    word_count,
)


@dataclass(frozen=True)
class BookSegment:
    segment_id: str
    title: str
    page_start: int
    page_end: int
    text: str
    keywords: list[str]


def split_text_blocks(text: str, max_block_words: int = 1800) -> list[str]:
    blocks: list[str] = []
    for raw_block in text.split("\n\n"):
        block = raw_block.strip()
        if not block:
            continue
        words = block.split()
        if len(words) <= max_block_words:
            blocks.append(block)
            continue
        for index in range(0, len(words), max_block_words):
            blocks.append(" ".join(words[index : index + max_block_words]))
    return blocks


def _title_from_blocks(blocks: list[str], keywords: list[str]) -> str:
    for block in blocks[:3]:
        if is_probable_heading(block):
            return shorten_text(block, 95)
    if keywords:
        pretty = ", ".join(keyword.capitalize() for keyword in keywords[:4])
        return shorten_text(f"Tema: {pretty}", 95)
    return "Segmento del libro"


def segment_pages(
    pages: list[dict[str, object]],
    target_min_words: int = 900,
    target_max_words: int = 3200,
) -> list[dict[str, object]]:
    segments: list[BookSegment] = []
    current_blocks: list[str] = []
    current_start: int | None = None
    current_end: int | None = None
    current_words = 0
    segment_index = 1

    def flush_segment() -> None:
        nonlocal current_blocks, current_start, current_end, current_words, segment_index
        if not current_blocks or current_start is None or current_end is None:
            return
        text = "\n\n".join(current_blocks).strip()
        keywords = extract_keywords(text)
        segments.append(
            BookSegment(
                segment_id=f"seg_{segment_index:04d}",
                title=_title_from_blocks(current_blocks, keywords),
                page_start=current_start,
                page_end=current_end,
                text=text,
                keywords=keywords,
            )
        )
        segment_index += 1
        current_blocks = []
        current_start = None
        current_end = None
        current_words = 0

    for page in pages:
        if not page.get("has_text"):
            continue
        page_number = int(page["page"])
        blocks = split_text_blocks(str(page.get("text", "")))
        for block in blocks:
            block_words = word_count(block)
            starts_new_topic = is_probable_heading(block)
            if current_blocks and starts_new_topic and current_words >= target_min_words:
                flush_segment()
            if current_blocks and current_words + block_words > target_max_words:
                flush_segment()
            if current_start is None:
                current_start = page_number
            current_end = page_number
            current_blocks.append(block)
            current_words += block_words
            if current_words >= target_max_words:
                flush_segment()

    flush_segment()
    return [asdict(segment) for segment in segments]
