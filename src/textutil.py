"""Small text helpers used across parsing, scoring, and PDF rendering."""

from __future__ import annotations

import re

_SLUG = re.compile(r"[^a-z0-9]+")
_TAGS = re.compile(r"(?is)<(script|style).*?>.*?</\1>")
_TAG = re.compile(r"(?s)<[^>]+>")
_WS = re.compile(r"\s+")


def slugify(value: str, *, fallback: str = "item", limit: int = 80) -> str:
    """Return a filesystem-safe slug."""
    slug = _SLUG.sub("_", value.lower()).strip("_")
    slug = slug[:limit].strip("_")
    return slug or fallback


def collapse_ws(value: str) -> str:
    return _WS.sub(" ", value).strip()


def contains_term(haystack: str, term: str) -> bool:
    """Match a skill term without treating short words as substrings of longer ones."""
    normalized_hay = haystack.lower().replace("-", " ").replace("/", " ")
    normalized_term = term.lower().replace("-", " ").replace("/", " ").strip()
    if not normalized_term:
        return False
    pattern = rf"(?<![a-z0-9+#]){re.escape(normalized_term)}(?:es|s)?(?![a-z0-9+#])"
    return re.search(pattern, normalized_hay) is not None


def html_to_text(html: str) -> str:
    without_blocks = _TAGS.sub(" ", html)
    without_tags = _TAG.sub(" ", without_blocks)
    return collapse_ws(without_tags)


def escape_xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def parse_keywords(value: str) -> list[str]:
    parts = re.split(r"[,;\n]", value)
    keywords: list[str] = []
    seen: set[str] = set()
    for part in parts:
        item = part.strip()
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            keywords.append(item)
    return keywords


def clip(value: str, limit: int = 500) -> str:
    text = collapse_ws(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."
