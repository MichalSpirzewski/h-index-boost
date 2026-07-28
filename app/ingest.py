"""Ingestion pipeline: DOI extraction/normalization, metadata APIs, background processing.

External API calls are isolated in small module-level functions (fetch_crossref,
fetch_semantic_scholar, fetch_unpaywall, download_pdf) so tests can monkeypatch them.
"""

import html
import json
import os
import re
import unicodedata
import uuid
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
import httpx
from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import db
from app.models import Article, ArticleAuthor, ArticleTopic, Author, Topic

CROSSREF_MAILTO = os.environ.get("CROSSREF_MAILTO", "guhard@gmail.com")
TITLE_SIMILARITY_THRESHOLD = 92

DOI_RE = re.compile(r'10\.\d{4,9}/[^\s"<>]+')
_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi:",
)


# --------------------------------------------------------------------------- DOI handling

def normalize_doi(raw: str) -> str:
    """Lowercase, strip resolver/`doi:` prefixes, trim whitespace."""
    s = raw.strip().lower()
    for prefix in _DOI_PREFIXES:
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return s.strip()


def _trim_doi_match(doi: str) -> str:
    """Strip punctuation that the regex drags in from surrounding prose."""
    doi = doi.rstrip(".,;:'\"")
    # A trailing ')' is part of the DOI only if it closes a '(' inside it.
    while doi.endswith(")") and doi.count("(") < doi.count(")"):
        doi = doi[:-1].rstrip(".,;:'\"")
    return doi


def extract_doi(text: str | None) -> str | None:
    """Find and normalize the first DOI in arbitrary text (prose, URL, pasted DOI)."""
    if not text:
        return None
    match = DOI_RE.search(text)
    if not match:
        return None
    return normalize_doi(_trim_doi_match(match.group(0)))


def extract_doi_from_pdf(pdf_bytes: bytes) -> str | None:
    """DOI from the first two pages of text, falling back to the metadata dict."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return None
    with doc:
        for page in doc.pages(0, min(2, doc.page_count)):
            doi = extract_doi(page.get_text())
            if doi:
                return doi
        for value in doc.metadata.values():
            doi = extract_doi(value)
            if doi:
                return doi
    return None


def pdf_title(pdf_bytes: bytes) -> str | None:
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            title = (doc.metadata.get("title") or "").strip()
            return title or None
    except Exception:
        return None


_MAX_PDF_TEXT = 500_000  # chars; keeps the FTS index bounded for scanned monsters


def extract_pdf_text(pdf_bytes: bytes) -> str | None:
    """Full text of every page, for the FTS index."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return None
    with doc:
        full_text = "\n".join(page.get_text() for page in doc).strip()
    return full_text[:_MAX_PDF_TEXT] or None


# --------------------------------------------------------------------------- abstract fallback

# Crossref only carries an `abstract` field when the publisher submits one — MDPI
# journals (e.g. Energies) do, but Elsevier/Springer/etc. usually don't. When
# that's missing, grep it straight out of the PDF: find the "Abstract" heading
# (allowing letter-spaced headings like "A B S T R A C T", common in two-column
# templates) and capture until the next section heading.
_ABSTRACT_RE = re.compile(
    r"a\s*b\s*s\s*t\s*r\s*a\s*c\s*t\s*[:.\-—–]?\s*\n?"
    r"(.+?)"
    r"(?=\n\s*(?:key\s*words?|highlights?|(?:\d+\.?\s*)?introduction\b"
    r"|©|http\S|article\s+info|received\b|1\.\s)|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_MIN_ABSTRACT_LEN = 40
_MAX_ABSTRACT_LEN = 3000


def _clean_abstract(raw: str) -> str:
    text = re.sub(r"\s*\n\s*", " ", raw).strip()
    text = re.sub(r"\s{2,}", " ", text)
    return text[:_MAX_ABSTRACT_LEN].strip()


def extract_abstract_from_pdf(pdf_bytes: bytes) -> str | None:
    """Best-effort abstract from a heading-delimited block on the first pages."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return None
    with doc:
        for page in doc.pages(0, min(3, doc.page_count)):
            match = _ABSTRACT_RE.search(page.get_text())
            if not match:
                continue
            candidate = _clean_abstract(match.group(1))
            if len(candidate) >= _MIN_ABSTRACT_LEN:
                return candidate
    return None


# --------------------------------------------------------------------------- keywords

# The label plus whatever follows it on the same line — which is empty when the
# publisher sets the keywords vertically, one per line (common in Elsevier PDFs).
_KEYWORD_LINE_RE = re.compile(
    r"^[ \t]*key\s*words?\s*[:\-—–][ \t]*(.*)$", re.IGNORECASE | re.MULTILINE
)
# Where a vertical keyword list ends: the abstract heading, a section number, etc.
_KEYWORD_BLOCK_END_RE = re.compile(
    r"^(a\s*b\s*s\s*t\s*r\s*a\s*c\s*t|a\s*r\s*t\s*i\s*c\s*l\s*e|highlights?"
    r"|©|https?:|\d+\.\s|introduction)\b",
    re.IGNORECASE,
)
_MAX_KEYWORDS = 10
_MAX_KEYWORD_LEN = 60
_MAX_KEYWORD_LINES = 12


def split_keywords(raw: str) -> list[str]:
    """Split an author-keyword string on common separators; dedupe case-insensitively."""
    keywords: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[;,·•\n]", raw):
        keyword = part.strip().rstrip(".").strip()
        if not keyword or len(keyword) > _MAX_KEYWORD_LEN:
            continue
        if keyword.lower() in seen:
            continue
        seen.add(keyword.lower())
        keywords.append(keyword)
    return keywords[:_MAX_KEYWORDS]


def keywords_after_label(text: str) -> list[str]:
    """Author keywords following a 'Keywords:' label, inline or set one per line."""
    match = _KEYWORD_LINE_RE.search(text)
    if not match:
        return []
    inline = match.group(1).strip()
    if inline:
        return split_keywords(inline)
    # Nothing on the label's own line: read the vertical list underneath it. The
    # per-keyword length cap in split_keywords discards prose if we overshoot.
    collected: list[str] = []
    for line in text[match.end():].splitlines()[:_MAX_KEYWORD_LINES]:
        stripped = line.strip()
        if not stripped:
            continue
        if _KEYWORD_BLOCK_END_RE.match(stripped):
            break
        collected.append(stripped)
    return split_keywords("\n".join(collected))


def extract_keywords_from_pdf(pdf_bytes: bytes) -> list[str]:
    """Author keywords from PDF metadata, else from a 'Keywords: ...' line up front."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return []
    with doc:
        meta = (doc.metadata.get("keywords") or "").strip()
        if meta:
            keywords = split_keywords(meta)
            if keywords:
                return keywords
        for page in doc.pages(0, min(2, doc.page_count)):
            keywords = keywords_after_label(page.get_text())
            if keywords:
                return keywords
    return []


# --------------------------------------------------------------------------- affiliations

# Institution words that mark a line as an affiliation rather than an author list.
# Stems (no trailing \b) so "Universi" also matches University/Universität/Università.
# "Academy/Academia/Akademia" are spelled out rather than stemmed to "Academ",
# which would also match the "Academic Editor:" line in MDPI front matter and
# start the block on the journal masthead instead of the real affiliation.
_AFF_KEYWORD_RE = re.compile(
    r"\b(Universi|Univerz|Institut|Instytut|Centrum|Department"
    r"|Laborator|Faculty|Fakult|School|College|Academy|Academia|Akadem"
    r"|Politech|Politecnico|Ministr|Hospital|Wydział|Division|GmbH"
    # "Cent(re|er)" is closed with a boundary rather than left open like the stems
    # above: as a stem it also fires inside "reliability-centered", which is title
    # wording, not an institution. A hyphen counts as a word boundary, so the
    # leading \b alone does not save us here.
    r"|Cent(?:re|er)s?\b)",
    re.IGNORECASE,
)
# An affiliation line prefixed by a superscript marker, e.g. "a National Centre…"
# or "1. Warsaw University…". Group 1 = marker, group 2 = affiliation text.
_MARKED_AFF_RE = re.compile(r"^\s*([a-z]|\d{1,2})[\s.,)]+(.+)$", re.IGNORECASE)
# Where the author/affiliation block ends (section headings, copyright, DOI line).
_AFF_BOUNDARY_RE = re.compile(
    r"^(a\s*b\s*s\s*t\s*r\s*a\s*c\s*t|a\s*r\s*t\s*i\s*c\s*l\s*e|keywords?|highlights?"
    r"|©|https?:|\d+\.\s|introduction)\b",
    re.IGNORECASE,
)
_MIN_AFF_LEN = 15
_MAX_AFF_LEN = 250


def _clean_affiliation(raw: str) -> str:
    text = re.sub(r"\s{2,}", " ", raw.strip()).strip(" ,;")
    return text[:_MAX_AFF_LEN].strip()


def _author_markers(region: str, full_name: str) -> list[str]:
    """Superscript markers (a, b, 1, 2…) trailing an author's name in the header block."""
    family = full_name.split()[-1] if full_name.split() else ""
    if not family:
        return []
    idx = region.find(family)
    if idx == -1:
        return []
    tail = region[idx + len(family):][:20]
    # Markers are lowercase letters/digits; author names are capitalized, so a
    # case-sensitive match stops the run before it bleeds into the next name.
    match = re.match(r"[\s*∗†,]*([a-z0-9](?:\s*,\s*[a-z0-9])*)", tail)
    if not match:
        return []
    return [m.strip().lower() for m in match.group(1).split(",") if m.strip()]


def extract_author_affiliations(
    pdf_bytes: bytes, author_names: list[str]
) -> list[str | None]:
    """Best-effort affiliation per author (aligned to `author_names` order).

    Reads the header block on page 1: collects affiliation lines (mapping any
    superscript markers), then either assigns the single affiliation to everyone,
    maps markers to authors, or — when it can't tell — shares all affiliations.
    Returns None for an author whose affiliation couldn't be resolved.
    """
    empty: list[str | None] = [None] * len(author_names)
    if not author_names:
        return empty
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            if doc.page_count == 0:
                return empty
            lines = doc[0].get_text().splitlines()
    except Exception:
        return empty

    marker_map: dict[str, str] = {}
    ordered_affs: list[str] = []
    first_aff_line = len(lines)
    in_aff_block = False  # once True, marker lines are affiliations regardless of language
    pending_marker: str | None = None
    for i, line in enumerate(lines[:40]):
        s = line.strip()
        if not s:
            continue
        if ordered_affs and _AFF_BOUNDARY_RE.match(s):
            break
        # Some publisher PDFs extract a superscript marker as its own line:
        # "a" followed by "National Centre...", rather than one combined line.
        if re.fullmatch(r"[a-z]|\d{1,2}", s, re.IGNORECASE):
            pending_marker = s.lower()
            continue
        marked = _MARKED_AFF_RE.match(s)
        # A marker-prefixed line is an affiliation if it looks like one (keyword) OR
        # we're already inside the block — a continuation like a Czech "b Fakulta …"
        # or "c Centrum výzkumu …" that carries no English institution keyword.
        if marked and (_AFF_KEYWORD_RE.search(marked.group(2)) or in_aff_block):
            aff = _clean_affiliation(marked.group(2))
            if len(aff) >= _MIN_AFF_LEN:
                marker_map[marked.group(1).lower()] = aff
                ordered_affs.append(aff)
                first_aff_line = min(first_aff_line, i)
                in_aff_block = True
        elif (
            _AFF_KEYWORD_RE.search(s)
            and len(s) >= _MIN_AFF_LEN
            and not s.lower().startswith("contents")
        ):
            aff = _clean_affiliation(s)
            ordered_affs.append(aff)
            if pending_marker is not None:
                marker_map[pending_marker] = aff
                pending_marker = None
            first_aff_line = min(first_aff_line, i)
            in_aff_block = True

    if not ordered_affs:
        return empty
    distinct = list(dict.fromkeys(ordered_affs))

    # One affiliation on the paper → it's everyone's.
    if len(distinct) == 1:
        return [distinct[0]] * len(author_names)

    # Multiple affiliations with markers → map each author via their markers.
    if marker_map:
        region = " ".join(line.strip() for line in lines[:first_aff_line])
        resolved: list[str | None] = []
        for name in author_names:
            affs = [marker_map[m] for m in _author_markers(region, name) if m in marker_map]
            resolved.append("; ".join(dict.fromkeys(affs)) or None)
        if any(resolved):
            return resolved

    # Multiple affiliations, can't map cleanly → share them all (best effort).
    return ["; ".join(distinct)] * len(author_names)


# --------------------------------------------------------------------------- storage

def save_pdf(pdf_bytes: bytes, doi: str | None) -> str:
    """Write PDF to disk; filename = normalized DOI (slashes -> _) or a UUID."""
    name = doi.replace("/", "_") if doi else uuid.uuid4().hex
    path = db.PDF_DIR / f"{name}.pdf"
    path.write_bytes(pdf_bytes)
    return str(path)


# --------------------------------------------------------------------------- external APIs

def fetch_crossref(doi: str) -> dict[str, Any] | None:
    try:
        resp = httpx.get(
            f"https://api.crossref.org/works/{doi}",
            params={"mailto": CROSSREF_MAILTO},
            timeout=30,
            follow_redirects=True,
        )
        if resp.status_code == 200:
            return resp.json()["message"]
    except Exception:
        pass
    return None


def fetch_semantic_scholar(doi: str) -> dict[str, Any] | None:
    try:
        resp = httpx.get(
            f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}",
            params={"fields": "abstract,fieldsOfStudy"},
            timeout=30,
            follow_redirects=True,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def fetch_unpaywall(doi: str) -> str | None:
    """Return an open-access PDF URL, if Unpaywall knows one."""
    try:
        resp = httpx.get(
            f"https://api.unpaywall.org/v2/{doi}",
            params={"email": CROSSREF_MAILTO},
            timeout=30,
            follow_redirects=True,
        )
        if resp.status_code == 200:
            location = resp.json().get("best_oa_location") or {}
            return location.get("url_for_pdf")
    except Exception:
        pass
    return None


def download_pdf(url: str) -> bytes | None:
    try:
        resp = httpx.get(url, timeout=60, follow_redirects=True)
        if resp.status_code == 200 and resp.content.startswith(b"%PDF"):
            return resp.content
    except Exception:
        pass
    return None


class _HighlightsParser(HTMLParser):
    """Small DOI-page parser: collect list items following a Highlights heading."""

    def __init__(self) -> None:
        super().__init__()
        self.in_heading = False
        self.heading_text: list[str] = []
        self.in_highlights = False
        self.in_item = False
        self.item_text: list[str] = []
        self.highlights: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("h1", "h2", "h3", "h4"):
            if self.in_highlights:
                self.in_highlights = False
            self.in_heading = True
            self.heading_text = []
        elif tag == "li" and self.in_highlights:
            self.in_item = True
            self.item_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("h1", "h2", "h3", "h4") and self.in_heading:
            heading = " ".join("".join(self.heading_text).split()).casefold()
            self.in_highlights = heading == "highlights"
            self.in_heading = False
        elif tag == "li" and self.in_item:
            item = " ".join("".join(self.item_text).split())
            if item and item not in self.highlights:
                self.highlights.append(item)
            self.in_item = False

    def handle_data(self, data: str) -> None:
        if self.in_heading:
            self.heading_text.append(data)
        elif self.in_item:
            self.item_text.append(data)


def extract_highlights_html(page_html: str) -> list[str]:
    parser = _HighlightsParser()
    parser.feed(page_html)
    return parser.highlights


def fetch_publisher_highlights(doi: str) -> list[str]:
    """One-time, user-triggered scrape of a DOI landing page."""
    try:
        response = httpx.get(
            f"https://doi.org/{doi}",
            follow_redirects=True,
            timeout=30,
            headers={
                "User-Agent": (
                    "RefBase/1.0 (one-time metadata retrieval; "
                    f"mailto:{CROSSREF_MAILTO})"
                )
            },
        )
        response.raise_for_status()
    except Exception:
        return []
    return extract_highlights_html(response.text)


# --------------------------------------------------------------------------- metadata mapping

def _strip_jats(abstract: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", abstract)).strip()


def normalize_journal_name(name: str) -> str:
    """Make HTML-encoded ampersands readable and stable in journal URLs."""
    decoded = html.unescape(name)
    return re.sub(r"\s*&\s*", " and ", decoded).strip()


def backfill_journal_names(session: Session) -> None:
    """Apply current journal normalization to records already in the library."""
    changed = False
    for article in session.scalars(
        select(Article).where(Article.journal.is_not(None))
    ):
        normalized = normalize_journal_name(article.journal or "")
        if normalized != article.journal:
            article.journal = normalized
            changed = True
    if changed:
        session.commit()


def _normalize_orcid(orcid: str | None) -> str | None:
    if not orcid:
        return None
    return orcid.strip().removeprefix("https://orcid.org/").removeprefix(
        "http://orcid.org/"
    ).upper() or None


def canonical_author(session: Session, author: Author) -> Author:
    """Follow the `merged_into_id` chain to the base (canonical) author."""
    seen: set[int] = set()
    while author.merged_into_id is not None and author.id not in seen:
        seen.add(author.id)
        parent = session.get(Author, author.merged_into_id)
        if parent is None:
            break
        author = parent
    return author


# Hard-coded author aliases: variant spellings that must collapse to one canonical
# name (case-insensitive key -> canonical display name). Applied before matching so
# every ingest of a variant groups under the same author record.
_NAME_ALIASES = {
    "mateusz marek nowak": "Mateusz Nowak",
}


def _apply_name_alias(full_name: str) -> str:
    return _NAME_ALIASES.get(full_name.strip().casefold(), full_name)


def backfill_dates(session: Session) -> int:
    """Fill published_date / online_date from already-stored Crossref JSON.

    Articles ingested before those columns existed only kept the year. The full
    message is on disk, so this needs no network call. Runs at startup; only
    touches rows where the field is still empty. Returns the number filled.
    """
    stale = session.scalars(
        select(Article).where(
            Article.crossref_json.is_not(None),
            (Article.published_date.is_(None)) | (Article.online_date.is_(None)),
        )
    ).all()
    filled = 0
    for article in stale:
        try:
            message = json.loads(article.crossref_json)
        except (TypeError, ValueError):
            continue
        published, online = crossref_dates(message)
        changed = False
        if published and not article.published_date:
            article.published_date = published
            changed = True
        if online and not article.online_date:
            article.online_date = online
            changed = True
        filled += changed
    if filled:
        session.commit()
    return filled


def apply_name_aliases(session: Session) -> int:
    """Fold already-stored variant spellings into their canonical author.

    `_apply_name_alias` only guards the write path, so authors ingested *before* an
    alias was added stay split — the reason a hard-coded alias looks like it does
    nothing. Runs at startup to make the alias table retroactive. Returns the number
    of authors merged away.
    """
    merged = 0
    for variant, canonical_name in _NAME_ALIASES.items():
        sources = [
            author
            for author in session.scalars(select(Author))
            if author.full_name.strip().casefold() == variant
            and author.merged_into_id is None
        ]
        if not sources:
            continue
        target = session.scalar(
            select(Author).where(Author.full_name == canonical_name).order_by(Author.id)
        )
        for source in sources:
            if target is None:  # only the variant exists — just rename it in place
                source.full_name = canonical_name
                target = source
                continue
            merge_authors(session, source, target)
            merged += 1
    if merged:
        session.commit()
    return merged


_NAME_TRANSLIT = str.maketrans({"ł": "l", "Ł": "L", "ø": "o", "đ": "d", "ß": "ss"})


def author_name_key(full_name: str) -> str:
    """Normalized identity for a person's name: case, diacritics and spacing folded.

    Not fuzzy matching — no similarity threshold. It treats "Sławomir Potempski"
    and "Slawomir Potempski" as the one person they are, which PDFs and Crossref
    spell inconsistently even for the same paper.
    """
    decomposed = unicodedata.normalize("NFKD", full_name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.translate(_NAME_TRANSLIT).lower().split())


def author_initial_surname_key(full_name: str) -> tuple[str, str] | None:
    """Return (first initial, surname), e.g. both Eleonora and E. -> (e, skrzypek)."""
    parts = author_name_key(full_name).split()
    if len(parts) < 2:
        return None
    first = re.sub(r"[^a-z0-9]", "", parts[0])
    surname = re.sub(r"[^a-z0-9]", "", parts[-1])
    if not first or not surname:
        return None
    return first[0], surname


def _uses_first_initial(full_name: str) -> bool:
    parts = author_name_key(full_name).split()
    return bool(parts) and len(re.sub(r"[^a-z0-9]", "", parts[0])) == 1


def merge_initial_authors(session: Session) -> int:
    """Fold abbreviated legacy names into one unambiguous full-name match."""
    authors = list(
        session.scalars(select(Author).where(Author.merged_into_id.is_(None)))
    )
    full_by_key: dict[tuple[str, str], list[Author]] = defaultdict(list)
    for author in authors:
        key = author_initial_surname_key(author.full_name)
        if key is not None and not _uses_first_initial(author.full_name):
            full_by_key[key].append(author)

    merged = 0
    for source in authors:
        if not _uses_first_initial(source.full_name):
            continue
        key = author_initial_surname_key(source.full_name)
        candidates = full_by_key.get(key, []) if key is not None else []
        if len(candidates) == 1:
            merge_authors(session, source, candidates[0])
            merged += 1
    if merged:
        session.commit()
    return merged


def merge_duplicate_authors(session: Session) -> int:
    """Fold author rows that denote the same person into one canonical record.

    Older ingest matched on ORCID alone, so a researcher credited with an ORCID on
    one paper and without one on another ended up as two rows — byte-identical
    names, separate publication counts. Current ingest cannot produce these, but
    the rows it already produced need clearing up. Runs at startup; merges are
    soft (`merged_into_id`), so nothing is destroyed. Returns the number merged.
    """
    groups: dict[str, list[Author]] = defaultdict(list)
    for author in session.scalars(select(Author).where(Author.merged_into_id.is_(None))):
        groups[author_name_key(author.full_name)].append(author)

    merged = 0
    for candidates in groups.values():
        if len(candidates) < 2:
            continue
        # Survivor: the richest record wins — an ORCID first, then the spelling that
        # kept its diacritics (the accented form is the correct one), then the oldest.
        candidates.sort(key=lambda a: (a.orcid is None, a.full_name.isascii(), a.id))
        target, *sources = candidates
        for source in sources:
            merge_authors(session, source, target)
            merged += 1
    if merged:
        session.commit()
    return merged


def _get_or_create_author(session: Session, full_name: str, orcid: str | None) -> Author:
    """Group an incoming author with an existing one when possible (v1, no fuzzy).

    Match order: hard-coded name alias, then ORCID, then exact normalized name
    (regardless of the existing row's ORCID). Any match resolves to its canonical
    author so contributions accrue to a single base record; a missing ORCID on the
    canonical is filled in.
    """
    full_name = _apply_name_alias(full_name)
    if orcid:
        author = session.scalar(select(Author).where(Author.orcid == orcid))
        if author:
            return canonical_author(session, author)
    existing = session.scalar(
        select(Author).where(Author.full_name == full_name).order_by(Author.id)
    )
    if existing:
        author = canonical_author(session, existing)
        if orcid and author.orcid is None:
            author.orcid = orcid
        return author

    # Crossref and PDFs sometimes provide only "E. Skrzypek". Resolve that to a
    # full name only when the initial + surname identifies exactly one candidate.
    incoming_key = author_initial_surname_key(full_name)
    if incoming_key is not None:
        candidates = [
            author
            for author in session.scalars(
                select(Author).where(Author.merged_into_id.is_(None))
            )
            if author_initial_surname_key(author.full_name) == incoming_key
            and _uses_first_initial(author.full_name) != _uses_first_initial(full_name)
        ]
        if len(candidates) == 1:
            author = candidates[0]
            if not _uses_first_initial(full_name):
                author.full_name = full_name
            if orcid and author.orcid is None:
                author.orcid = orcid
            return author
    author = Author(full_name=full_name, orcid=orcid)
    session.add(author)
    session.flush()
    return author


def merge_authors(session: Session, source: Author, target: Author) -> int:
    """Fold `source` into `target`: move article links, then soft-mark source merged.

    Returns the number of article links moved. Links to articles the target already
    authors are dropped (the composite PK forbids duplicates). No rows are deleted —
    `source` stays put with `merged_into_id` set, so the merge is auditable/reversible.
    """
    source = canonical_author(session, source)
    target = canonical_author(session, target)
    if source.id == target.id:
        return 0

    target_article_ids = {
        aid for (aid,) in session.execute(
            select(ArticleAuthor.article_id).where(ArticleAuthor.author_id == target.id)
        )
    }
    moved = 0
    links = session.scalars(
        select(ArticleAuthor).where(ArticleAuthor.author_id == source.id)
    ).all()
    for link in links:
        if link.article_id in target_article_ids:
            session.delete(link)  # target already credited on this article
            continue
        # author_id is part of the composite PK, so re-point via delete + insert.
        article_id, position = link.article_id, link.position
        session.delete(link)
        session.flush()
        session.add(ArticleAuthor(article_id=article_id, author_id=target.id, position=position))
        target_article_ids.add(article_id)
        moved += 1

    # Preserve an ORCID that would otherwise be lost. orcid is UNIQUE, so the source
    # must be cleared and flushed *before* the value is assigned to the target,
    # otherwise both rows momentarily hold it and the flush hits the constraint.
    if target.orcid is None and source.orcid:
        orcid = source.orcid
        source.orcid = None
        session.flush()
        target.orcid = orcid

    source.merged_into_id = target.id
    session.flush()
    return moved


def _get_or_create_topic(session: Session, name: str) -> Topic:
    topic = session.scalar(select(Topic).where(Topic.name == name))
    if topic is None:
        topic = Topic(name=name)
        session.add(topic)
        session.flush()
    return topic


def iso_date_from_parts(date_parts: list | None) -> str | None:
    """Crossref `date-parts` -> zero-padded ISO string at the given precision.

    [2025, 11, 11] -> "2025-11-11", [2026, 5] -> "2026-05", [2022] -> "2022".
    Publishers routinely date an issue to a month only, so the missing day is left
    off rather than padded to a day the paper was never published on.
    """
    if not date_parts:
        return None
    parts = [int(p) for p in date_parts[:3] if p is not None]
    if not parts:
        return None
    if len(parts) == 1:
        return f"{parts[0]:04d}"
    if len(parts) == 2:
        return f"{parts[0]:04d}-{parts[1]:02d}"
    return f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"


def _first_date_parts(message: dict[str, Any], *keys: str) -> list | None:
    """First present `date-parts` among the given Crossref date fields."""
    for key in keys:
        parts = (message.get(key) or {}).get("date-parts") or [[]]
        if parts[0]:
            return parts[0]
    return None


def crossref_dates(message: dict[str, Any]) -> tuple[str | None, str | None]:
    """(issue date, first-online date) from one Crossref message.

    `issued` is the citable publication date and the one that belongs in a
    bibliography. `created` is when the DOI was registered, which tracks the
    publisher's "Available online" line — earlier than the issue, and a different
    thing entirely, so it is kept in its own field rather than mixed in.
    """
    published = iso_date_from_parts(
        _first_date_parts(message, "issued", "published", "published-print")
    )
    online = iso_date_from_parts(
        _first_date_parts(message, "published-online", "created")
    )
    return published, online


def apply_crossref(session: Session, article: Article, message: dict[str, Any]) -> None:
    article.crossref_json = json.dumps(message)
    titles = message.get("title") or []
    if titles:
        article.title = titles[0]
    containers = message.get("container-title") or []
    if containers:
        article.journal = normalize_journal_name(containers[0])
    date_parts = (message.get("issued") or {}).get("date-parts") or [[]]
    if date_parts[0] and date_parts[0][0]:
        article.year = int(date_parts[0][0])
    published, online = crossref_dates(message)
    if published:
        article.published_date = published
    if online:
        article.online_date = online
    if message.get("abstract"):
        article.abstract = _strip_jats(message["abstract"])

    session.query(ArticleAuthor).filter_by(article_id=article.id).delete()
    seen_author_ids: set[int] = set()
    position = 0
    for entry in message.get("author") or []:
        full_name = " ".join(x for x in (entry.get("given"), entry.get("family")) if x).strip()
        if not full_name:
            continue
        author = _get_or_create_author(session, full_name, _normalize_orcid(entry.get("ORCID")))
        if author.id in seen_author_ids:
            continue
        seen_author_ids.add(author.id)
        affiliation = "; ".join(
            a["name"].strip() for a in (entry.get("affiliation") or []) if a.get("name")
        ) or None
        session.add(
            ArticleAuthor(
                article_id=article.id,
                author_id=author.id,
                position=position,
                affiliation=affiliation,
            )
        )
        position += 1

    for subject in message.get("subject") or []:
        _link_topic(session, article, subject)


def _link_topic(session: Session, article: Article, name: str) -> None:
    name = name.strip()
    if not name:
        return
    topic = _get_or_create_topic(session, name)
    exists = session.scalar(
        select(ArticleTopic).where(
            ArticleTopic.article_id == article.id, ArticleTopic.topic_id == topic.id
        )
    )
    if not exists:
        session.add(ArticleTopic(article_id=article.id, topic_id=topic.id))


def apply_semantic_scholar(session: Session, article: Article, data: dict[str, Any]) -> None:
    if not article.abstract and data.get("abstract"):
        article.abstract = data["abstract"]
    for field in data.get("fieldsOfStudy") or []:
        _link_topic(session, article, field)


def add_pdf_keywords(session: Session, article: Article, pdf_bytes: bytes) -> list[str]:
    """Extract author keywords from an uploaded PDF and link them as topics."""
    keywords = extract_keywords_from_pdf(pdf_bytes)
    for keyword in keywords:
        _link_topic(session, article, keyword)
    if keywords:
        session.commit()
    return keywords


def apply_pdf_affiliations(session: Session, article: Article) -> int:
    """Fill missing per-author affiliations for an article from its stored PDF.

    Never overrides an affiliation already set (e.g. from Crossref). Returns the
    number of author links filled. Must run *after* apply_crossref (which creates
    the author links) since PDF parsing has no authors to attach to on its own.
    """
    if not article.pdf_path:
        return 0
    links = (
        session.scalars(
            select(ArticleAuthor)
            .where(ArticleAuthor.article_id == article.id)
            .order_by(ArticleAuthor.position)
        ).all()
    )
    if not links or all(link.affiliation for link in links):
        return 0
    try:
        pdf_bytes = Path(article.pdf_path).read_bytes()
    except OSError:
        return 0

    names = [session.get(Author, link.author_id).full_name for link in links]
    affiliations = extract_author_affiliations(pdf_bytes, names)
    filled = 0
    for link, affiliation in zip(links, affiliations, strict=False):
        if not link.affiliation and affiliation:
            link.affiliation = affiliation
            filled += 1
    if filled:
        session.commit()
    return filled


def repair_shared_pdf_affiliations(session: Session) -> int:
    """Repair legacy rows where a failed marker parse gave everyone every affiliation.

    Only suspicious articles are touched: multiple authors all have the identical
    semicolon-joined value, and a fresh parse now resolves at least two distinct
    author affiliations.
    """
    repaired = 0
    articles = session.scalars(
        select(Article).where(Article.pdf_path.is_not(None))
    ).all()
    for article in articles:
        links = session.scalars(
            select(ArticleAuthor)
            .where(ArticleAuthor.article_id == article.id)
            .order_by(ArticleAuthor.position)
        ).all()
        stored = {link.affiliation for link in links if link.affiliation}
        if len(links) < 2 or len(stored) != 1:
            continue
        shared = next(iter(stored), "")
        if "; " not in shared:
            continue
        try:
            pdf_bytes = Path(article.pdf_path or "").read_bytes()
        except OSError:
            continue
        parsed = extract_author_affiliations(
            pdf_bytes, [link.author.full_name for link in links]
        )
        resolved = {affiliation for affiliation in parsed if affiliation}
        if len(resolved) < 2:
            continue
        for link, affiliation in zip(links, parsed, strict=False):
            if affiliation and link.affiliation != affiliation:
                link.affiliation = affiliation
                repaired += 1
    if repaired:
        session.commit()
    return repaired


def _backfill_abstract(article: Article, pdf_bytes: bytes) -> None:
    """Fill a missing abstract from the PDF; never override a real one from Crossref/S2."""
    if article.abstract:
        return
    abstract = extract_abstract_from_pdf(pdf_bytes)
    if abstract:
        article.abstract = abstract


def attach_pdf(session: Session, article: Article, pdf_bytes: bytes) -> None:
    """Store a PDF for an article: file on disk, full text for search, keywords as topics."""
    article.pdf_path = save_pdf(pdf_bytes, article.doi)
    article.pdf_text = extract_pdf_text(pdf_bytes)
    _backfill_abstract(article, pdf_bytes)
    session.commit()
    add_pdf_keywords(session, article, pdf_bytes)


def rescan_article_pdf(session: Session, article: Article) -> list[str]:
    """Re-run the scrub on an already-stored PDF: refresh full text, link keywords."""
    if not article.pdf_path:
        return []
    try:
        pdf_bytes = Path(article.pdf_path).read_bytes()
    except OSError:
        return []
    article.pdf_text = extract_pdf_text(pdf_bytes)
    _backfill_abstract(article, pdf_bytes)
    session.commit()
    keywords = add_pdf_keywords(session, article, pdf_bytes)
    apply_pdf_affiliations(session, article)
    return keywords


# --------------------------------------------------------------------------- duplicates

def find_similar_titles(
    session: Session, title: str, threshold: int = TITLE_SIMILARITY_THRESHOLD
) -> list[dict[str, Any]]:
    """Soft duplicate check for DOI-less papers: fuzzy match against existing titles."""
    matches = []
    rows = session.execute(select(Article.id, Article.title).where(Article.title.is_not(None)))
    for article_id, existing_title in rows:
        score = fuzz.token_sort_ratio(title.lower(), existing_title.lower())
        if score >= threshold:
            matches.append({"article_id": article_id, "title": existing_title, "score": round(score, 1)})
    return matches


# --------------------------------------------------------------------------- background job

def process_article(article_id: int) -> None:
    """BackgroundTasks entry point: fetch metadata for an article that has a DOI.

    Crossref is required (failure -> metadata_failed); Semantic Scholar and
    Unpaywall are best-effort enrichment and must never fail the job.
    """
    with db.SessionLocal() as session:
        article = session.get(Article, article_id)
        if article is None or article.doi is None:
            return

        if article.crossref_json:  # never re-fetch a stored DOI
            message = json.loads(article.crossref_json)
        else:
            message = fetch_crossref(article.doi)

        if message is None:
            article.status = "metadata_failed"
            session.commit()
            return

        apply_crossref(session, article, message)

        try:
            s2 = fetch_semantic_scholar(article.doi)
            if s2:
                apply_semantic_scholar(session, article, s2)
        except Exception:
            pass

        try:
            if not article.pdf_path:
                oa_url = fetch_unpaywall(article.doi)
                if oa_url:
                    pdf_bytes = download_pdf(oa_url)
                    if pdf_bytes:
                        attach_pdf(session, article, pdf_bytes)
        except Exception:
            pass

        # Fill any author affiliations Crossref didn't provide from the PDF header.
        try:
            apply_pdf_affiliations(session, article)
        except Exception:
            pass

        article.status = "ready"
        session.commit()
