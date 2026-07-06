from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from typing import Iterable

SPANISH_STOPWORDS = {
    "ademas", "al", "algo", "ante", "antes", "aquel", "aquella", "aquellas", "aquellos",
    "asi", "cada", "como", "con", "contra", "cual", "cuando", "de", "del", "desde", "donde",
    "dos", "durante", "e", "el", "ella", "ellas", "ellos", "en", "entre", "era", "eran", "es",
    "esa", "esas", "ese", "eso", "esos", "esta", "estan", "estar", "estas", "este", "esto",
    "estos", "fue", "han", "hasta", "hay", "la", "las", "le", "les", "lo", "los", "mas",
    "mediante", "muy", "no", "o", "para", "pero", "por", "porque", "que", "se", "sea", "ser",
    "si", "sin", "sobre", "son", "su", "sus", "tambien", "te", "tener", "tiene", "todo",
    "todos", "tras", "un", "una", "unas", "uno", "unos", "y", "ya",
}

TOKEN_RE = re.compile(r"[a-zA-ZáéíóúÁÉÍÓÚñÑüÜ0-9]+")


def strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value)
    return "".join(char for char in normalized if unicodedata.category(char) != "Mn")


def normalize_for_matching(value: str) -> str:
    value = strip_accents(value).lower()
    value = re.sub(r"[^a-z0-9ñ\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def fix_broken_hyphenation(text: str) -> str:
    return re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)


def is_probable_heading(line: str) -> bool:
    line = re.sub(r"\s+", " ", line).strip()
    if not line or len(line) > 110:
        return False
    words = line.split()
    if not 1 <= len(words) <= 14:
        return False
    if line.endswith((".", ";", ",")):
        return False
    alpha = [char for char in line if char.isalpha()]
    if not alpha:
        return False
    uppercase_ratio = sum(1 for char in alpha if char.isupper()) / len(alpha)
    starts_with_number = bool(re.match(r"^\d+(\.\d+)*\s+", line))
    titlecase_words = sum(1 for word in words if word[:1].isupper())
    return uppercase_ratio > 0.55 or starts_with_number or titlecase_words >= max(2, len(words) // 2)


def join_wrapped_lines(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    paragraphs: list[str] = []
    current: list[str] = []

    def flush_current() -> None:
        if current:
            paragraphs.append(" ".join(current).strip())
            current.clear()

    for line in lines:
        if not line:
            flush_current()
            continue
        if is_probable_heading(line):
            flush_current()
            paragraphs.append(line)
            continue
        current.append(line)
    flush_current()
    return "\n\n".join(part for part in paragraphs if part)


def clean_extracted_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = fix_broken_hyphenation(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = join_wrapped_lines(text)
    return re.sub(r"[ \t]+", " ", text).strip()


def tokenize(value: str) -> list[str]:
    normalized = normalize_for_matching(value)
    tokens = TOKEN_RE.findall(normalized)
    return [token for token in tokens if len(token) > 2 and token not in SPANISH_STOPWORDS and not token.isdigit()]


def extract_keywords(text: str, max_keywords: int = 8) -> list[str]:
    counts = Counter(tokenize(text))
    return [token for token, _count in counts.most_common(max_keywords)]


def word_count(text: str) -> int:
    return len(TOKEN_RE.findall(text))


def shorten_text(value: str, max_length: int) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= max_length:
        return value
    cutoff = value[: max_length - 3].rsplit(" ", 1)[0]
    return f"{cutoff or value[: max_length - 3]}..."


def hash_text_fragment(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text).strip().encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def unique_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
