"""BibTeX generation from stored Crossref JSON (fallback: the Article row itself).

Cite key = firstauthorlastnameYEARfirstword, deduplicated with a/b/c suffixes
within one export. Unicode is kept (biblatex-friendly); {, }, %, &, # are escaped.
"""

import json
import re
import unicodedata
from typing import Any

from app.models import Article

_TYPE_MAP = {
    "journal-article": "article",
    "proceedings-article": "inproceedings",
    "book-chapter": "incollection",
    "book": "book",
    "monograph": "book",
    "edited-book": "book",
    "report": "techreport",
    "dissertation": "phdthesis",
}


def escape_bibtex(value: str) -> str:
    return re.sub(r"([{}%&#])", r"\\\1", value)


def _slug(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", ascii_value.lower())


def _first_word(title: str | None) -> str:
    for word in re.split(r"\s+", title or ""):
        if _slug(word):
            return word
    return ""


def make_cite_key(last_name: str | None, year: int | str | None, title: str | None) -> str:
    return "{}{}{}".format(
        _slug(last_name or "") or "anon",
        year or "nd",
        _slug(_first_word(title)),
    )


def _prepare(article: Article) -> tuple[str, str, dict[str, Any]]:
    """Return (base cite key, entry type, fields) for one article."""
    if article.crossref_json:
        message = json.loads(article.crossref_json)
        author_entries = message.get("author") or []
        names = [
            " ".join(x for x in (a.get("given"), a.get("family")) if x)
            for a in author_entries
        ]
        first_last = author_entries[0].get("family") if author_entries else None
        date_parts = (message.get("issued") or {}).get("date-parts") or [[]]
        year = date_parts[0][0] if date_parts[0] else None
        title = (message.get("title") or [None])[0]
        entry_type = _TYPE_MAP.get(message.get("type", ""), "misc")
        fields: dict[str, Any] = {
            "author": " and ".join(n for n in names if n) or None,
            "title": title,
            "journal": (message.get("container-title") or [None])[0],
            "year": year,
            "volume": message.get("volume"),
            "number": message.get("issue"),
            "pages": message.get("page"),
            "doi": message.get("DOI"),
            "publisher": message.get("publisher"),
            "url": message.get("URL"),
        }
    else:
        names = [author.full_name for author in article.authors]
        first_last = names[0].split()[-1] if names and names[0].split() else None
        year = article.year
        title = article.title
        entry_type = "misc"
        fields = {
            "author": " and ".join(names) or None,
            "title": title,
            "journal": article.journal,
            "year": year,
            "doi": article.doi,
            "url": article.source_url,
        }
    return make_cite_key(first_last, year, title), entry_type, fields


def _render(key: str, entry_type: str, fields: dict[str, Any]) -> str:
    lines = [f"@{entry_type}{{{key},"]
    for name, value in fields.items():
        if value in (None, ""):
            continue
        lines.append(f"  {name} = {{{escape_bibtex(str(value))}}},")
    lines[-1] = lines[-1].rstrip(",")
    lines.append("}")
    return "\n".join(lines)


def unique_key(base_key: str, used: set[str]) -> str:
    """Add an a/b/c suffix until the key is free, then claim it in `used`."""
    key = base_key
    suffix = 0
    while key in used:
        key = base_key + chr(ord("a") + suffix)
        suffix += 1
    used.add(key)
    return key


def export_entries(articles: list[Article]) -> list[tuple[str, str]]:
    """One (cite key, rendered entry) pair per article, keys deduplicated across the
    whole list. Keeping entries separate also gives summary exports a stable cite
    key for each displayed BibTeX block and PDF filename."""
    used: set[str] = set()
    entries = []
    for article in articles:
        base_key, entry_type, fields = _prepare(article)
        key = unique_key(base_key, used)
        entries.append((key, _render(key, entry_type, fields)))
    return entries


def export_bibtex(articles: list[Article]) -> str:
    """Render articles as one .bib document, deduplicating cite keys with a/b/c suffixes."""
    return "\n\n".join(entry for _key, entry in export_entries(articles)) + "\n"


def article_bibtex(article: Article) -> str:
    return export_bibtex([article])


def pdf_filename(article: Article) -> str:
    """Human-readable download filename, e.g. smith2020quantum.pdf."""
    base_key, _, _ = _prepare(article)
    return f"{base_key}.pdf"
