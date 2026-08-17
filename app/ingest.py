"""Ingestion pipeline: DOI extraction/normalization, metadata APIs, background processing.

External API calls are isolated in small module-level functions (fetch_crossref,
fetch_semantic_scholar, fetch_unpaywall, download_pdf) so tests can monkeypatch them.
"""

import hashlib
import html
import json
import os
import re
import unicodedata
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
import httpx
from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import db
from app.models import Article, ArticleAuthor, ArticleKeyword, Author, Keyword

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
    """The paper's title: the PDF metadata field, else the front page's own typography."""
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            title = (doc.metadata.get("title") or "").strip()
    except Exception:
        return None
    # TeX-produced PDFs (and plenty of publisher ones) leave the metadata title
    # empty, which is how DOI-less uploads end up stored with no title at all.
    return title or extract_title_from_pdf(pdf_bytes)


# --------------------------------------------------------------------------- front-page layout

# Flat text extraction glues affiliation markers onto the words they follow —
# "Maciej Skrzypek" + superscript "a" comes back as "Maciej Skrzypeka". The
# layout dict keeps them apart: markers are their own spans, flagged superscript
# and set several points smaller than the byline. Reading the spans instead of
# the text is what makes the title and the author list recoverable.
_SUPERSCRIPT_FLAG = 1  # PyMuPDF span flag bit 0
_SUPERSCRIPT_SIZE_DROP = 1.0  # pt below the line's own size ⇒ treat as a marker


def _first_page_lines(pdf_bytes: bytes) -> list[dict]:
    """Layout lines of page 1, in reading order (empty when the PDF won't open)."""
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            if doc.page_count == 0:
                return []
            return [
                line
                for block in doc[0].get_text("dict")["blocks"]
                if block.get("type") == 0  # 0 = text, 1 = image
                for line in block.get("lines", [])
            ]
    except Exception:
        return []


def _spans(line: dict) -> list[dict]:
    return [span for span in line.get("spans", []) if span["text"].strip()]


def _line_size(line: dict) -> float:
    return max((span["size"] for span in _spans(line)), default=0.0)


def _line_text(line: dict, *, drop_markers: bool = False) -> str:
    """One line's text. `drop_markers` discards superscript spans (∗, a, b, 1)."""
    spans = _spans(line)
    if drop_markers and spans:
        size = max(span["size"] for span in spans)
        spans = [
            span
            for span in spans
            if not span["flags"] & _SUPERSCRIPT_FLAG
            and span["size"] > size - _SUPERSCRIPT_SIZE_DROP
        ]
    # Spans carry their own leading/trailing spaces, so they butt together.
    return re.sub(r"\s{2,}", " ", "".join(span["text"] for span in spans)).strip()


_MIN_TITLE_LEN = 15
_MAX_TITLE_LEN = 300
# Mastheads and running heads can be set as large as the title; none of them are one.
# Front matter that shares the masthead with the journal line and is sometimes set
# as large as the title: neither a title nor a journal name.
_MASTHEAD_NOISE_RE = re.compile(
    r"^(open access|journal homepage|homepage|https?:|www\.|doi\b|vol\.|volume\b"
    r"|issue\b|issn|isbn|received\b|revised\b|accepted\b|published\b|available\b"
    r"|downloaded\b|copyright\b|©|article\b|preprint\b|page\b)",
    re.IGNORECASE,
)


def _title_lines(lines: list[dict]) -> tuple[list[str], int, int]:
    """The largest-face run of lines on page 1, and the span of indices it covers.

    Publishers set the title larger than everything around it — masthead above,
    byline below — so the first run at the page's largest size is the title,
    however many lines it wraps onto. Indices are into the non-empty lines: what
    precedes the run is the masthead, what follows it opens the byline.
    """
    sized = [(line, _line_size(line)) for line in lines if _line_text(line)]
    if not sized:
        return [], 0, 0
    largest = max(size for _line, size in sized)
    collected: list[str] = []
    start = end = 0
    for index, (line, size) in enumerate(sized):
        text = _line_text(line)
        if abs(size - largest) < 0.5 and not _MASTHEAD_NOISE_RE.match(text):
            if not collected:
                start = index
            collected.append(text)
            end = index + 1
        elif collected:
            break  # the run is over; a same-size heading further down is not the title
    return collected, start, end


def extract_title_from_pdf(pdf_bytes: bytes) -> str | None:
    """Best-effort title from the front page's typography."""
    collected, _start, _end = _title_lines(_first_page_lines(pdf_bytes))
    title = " ".join(collected).strip()
    if not _MIN_TITLE_LEN <= len(title) <= _MAX_TITLE_LEN:
        return None
    return title


_MIN_JOURNAL_LEN = 6
_MAX_JOURNAL_LEN = 120
# The volume/year/page apparatus trailing a journal name in a running head: the
# first standalone number, bare ("… Technologies 94"), parenthesised ("… (2014)")
# or comma-led ("Energies 2023, 16").
_JOURNAL_TAIL_RE = re.compile(r"[\s,;]+\(?\d")
# What is left has to read as a name — letters and ordinary title punctuation.
_JOURNAL_NAME_RE = re.compile(r"^[^\W\d_][\w\s&:\-—–'’.()/]*$", re.UNICODE)


def extract_journal_from_pdf(pdf_bytes: bytes) -> str | None:
    """The journal name from the running head printed above the title.

    Publishers head page 1 with a citation line — "Journal of Power Technologies
    94 (Nuclear Issue) (2014) 41–50", "Nuclear Engineering and Design 380 (2021)
    111234", "Energies 2023, 16, 4567" — whose leading text is the journal and
    whose volume/issue/year/pages identify it as that kind of line. A masthead
    carrying no such numbers is left alone rather than guessed at.
    """
    lines = _first_page_lines(pdf_bytes)
    _title, title_start, _end = _title_lines(lines)
    texts = [text for text in (_line_text(line) for line in lines) if text]
    for text in texts[:title_start]:
        if _MASTHEAD_NOISE_RE.match(text):
            continue
        tail = _JOURNAL_TAIL_RE.search(text)
        if tail is None:
            continue
        name = normalize_journal_name(text[: tail.start()].strip(" ,;.-"))
        if _MIN_JOURNAL_LEN <= len(name) <= _MAX_JOURNAL_LEN and _JOURNAL_NAME_RE.match(name):
            return name
    return None


_MAX_PDF_AUTHORS = 40
_MAX_BYLINE_LINES = 3
_AUTHOR_SPLIT_RE = re.compile(r",|;| and | & ", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\S+@\S+")
# Marker glyphs and stray digits that survive alongside a name.
_AUTHOR_NOISE_RE = re.compile(r"[∗*†‡§¶#0-9]+")


def _clean_author_name(raw: str) -> str | None:
    name = re.sub(r"\s{2,}", " ", _AUTHOR_NOISE_RE.sub("", raw)).strip(" ,.;:-")
    tokens = name.split()
    # Two to five tokens, opening and closing with a capital: enough to keep
    # "van der Meer" while rejecting leftover markers and "Corresponding author".
    if not 2 <= len(tokens) <= 5 or not 4 <= len(name) <= 80:
        return None
    if not (tokens[0][:1].isupper() and tokens[-1][:1].isupper()):
        return None
    return name


def extract_authors_from_pdf(pdf_bytes: bytes) -> list[str]:
    """Author names from the byline under the title.

    For papers Crossref has no record of, this is the only place an author list
    can come from. It reads the byline's spans rather than its text so that the
    superscript affiliation markers drop out instead of fusing onto surnames.
    """
    lines = _first_page_lines(pdf_bytes)
    _title, _start, start = _title_lines(lines)
    if not start:
        return []

    names: list[str] = []
    seen: set[str] = set()
    for line in [line for line in lines if _line_text(line)][start:][:_MAX_BYLINE_LINES]:
        text = _line_text(line)
        # The byline ends where the affiliations, the abstract or a contact address begins.
        if (
            _AFF_KEYWORD_RE.search(text)
            or _AFF_BOUNDARY_RE.match(text)
            or _EMAIL_RE.search(text)
        ):
            break
        for candidate in _AUTHOR_SPLIT_RE.split(_line_text(line, drop_markers=True)):
            name = _clean_author_name(candidate)
            if name and author_name_key(name) not in seen:
                seen.add(author_name_key(name))
                names.append(name)
        if names:  # a byline that produced names does not continue past its line
            break
    return names[:_MAX_PDF_AUTHORS]


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
# A keyword line ending in a separator has been broken by the column width.
_KEYWORD_CONTINUES = (",", ";", "·", "•")


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
        # A trailing separator means the list ran past the width of the line and
        # continues below — anything else means it finished where it started.
        if not inline.endswith(_KEYWORD_CONTINUES):
            return split_keywords(inline)
        collected = [inline]
        for line in text[match.end():].splitlines()[:_MAX_KEYWORD_LINES]:
            stripped = line.strip()
            if not stripped:
                continue
            if _KEYWORD_BLOCK_END_RE.match(stripped):
                break
            collected.append(stripped)
            if not stripped.endswith(_KEYWORD_CONTINUES):
                break  # this line closed the list
        return split_keywords("\n".join(collected))
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
# An affiliation line prefixed by a superscript marker, e.g. "a National Centre…",
# "1. Warsaw University…" or — when text extraction fuses the superscript onto the
# word behind it — "aInstitute of Heat Engineering…". Group 1 = marker, group 2 =
# affiliation text.
#
# Letter markers must be lowercase, which is how they are typeset and what keeps
# "A. Sołtana 7, 05-400, Otwock" (a street address continuing the affiliation
# above) from being read as marker "a" pointing at a new institution.
_MARKED_AFF_RE = re.compile(r"^\s*([a-z]|\d{1,2})(?:[\s.,)]+|(?=[A-Z]))(.+)$")
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


# Accents that publisher fonts emit as their own character instead of composing
# them into the letter: Elsevier renders "Prusiński" as "Prusi´nski" (U+00B4 then
# a bare "n"). NFKD turns these into a space plus a combining mark, so they need
# dropping outright rather than decomposing, or the fold leaves a stray gap.
_SPACING_ACCENTS = frozenset("`^~¨¯´¸ˆˇ˘˙˚˛˜˝")
# Letters with no NFKD decomposition; without these, Polish "ł" or Nordic "ø"
# would survive the fold and still fail to match their ASCII rendering.
_LETTER_FOLDS = {
    "ł": "l", "đ": "d", "ð": "d", "ø": "o", "æ": "ae", "œ": "oe",
    "ß": "ss", "þ": "th", "ħ": "h", "ı": "i", "ŋ": "n",
}


def _fold_diacritics(text: str) -> tuple[str, list[int]]:
    """Strip accents from `text`, returning the folded string and, per folded
    character, the index it came from in the original. The index map lets callers
    slice the *original* text around a match found in the folded one."""
    folded: list[str] = []
    origin: list[int] = []
    for i, char in enumerate(text):
        if char in _SPACING_ACCENTS:
            continue
        replacement = _LETTER_FOLDS.get(char.lower())
        if replacement is not None:
            replacement = replacement.upper() if char.isupper() else replacement
        else:
            decomposed = unicodedata.normalize("NFKD", char)
            replacement = "".join(c for c in decomposed if not unicodedata.combining(c))
        for out in replacement:
            folded.append(out)
            origin.append(i)
    return "".join(folded), origin


def _author_markers(region: str, full_name: str) -> list[str]:
    """Superscript markers (a, b, 1, 2…) trailing an author's name in the header block."""
    family = full_name.split()[-1] if full_name.split() else ""
    if not family:
        return []
    # Match on the accent-folded forms: Crossref stores precomposed characters
    # ("Prusiński") while the PDF may spell the same name with a detached accent,
    # and a literal substring search would miss every such author.
    folded_region, origin = _fold_diacritics(region)
    folded_family, _ = _fold_diacritics(family)
    if not folded_family:
        return []
    idx = folded_region.find(folded_family)
    if idx == -1:
        return []
    # Markers follow the last character of the name in the *original* string.
    tail = region[origin[idx + len(folded_family) - 1] + 1:][:20]
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


def pdf_sha256(pdf_bytes: bytes) -> str:
    """Content fingerprint of an uploaded PDF."""
    return hashlib.sha256(pdf_bytes).hexdigest()


def find_by_pdf_hash(session: Session, pdf_bytes: bytes) -> Article | None:
    """A visible article already holding this exact file, if there is one.

    The paper's DOI is the primary duplicate guard, but a paper without one — no
    DOI in the text, none registered — has nothing to be unique on, so the same
    PDF can be uploaded any number of times. Hashing the bytes closes that.

    Hidden records are ignored on purpose: pointing an uploader at a soft-deleted
    article would hand them a link that 404s, and re-adding a paper somebody
    removed is a legitimate thing to do.
    """
    return session.scalar(
        select(Article)
        .where(
            Article.pdf_sha256 == pdf_sha256(pdf_bytes),
            Article.hidden.is_(False),
        )
        .order_by(Article.id)
    )


def backfill_pdf_hashes(session: Session) -> int:
    """Fingerprint PDFs stored before the column existed. Returns rows filled.

    Reads each file once and then never again — unlike the affiliation backfills,
    a row whose file is on disk always ends up with a hash, so the work does not
    repeat on the next startup.
    """
    stale = session.scalars(
        select(Article).where(
            Article.pdf_path.is_not(None), Article.pdf_sha256.is_(None)
        )
    ).all()
    filled = 0
    for article in stale:
        pdf_bytes = read_stored_pdf(article.pdf_path)
        if pdf_bytes is None:
            continue
        article.pdf_sha256 = pdf_sha256(pdf_bytes)
        filled += 1
    if filled:
        session.commit()
    return filled


def read_stored_pdf(pdf_path: str | None) -> bytes | None:
    """Bytes of a stored PDF, or None when the row has no path or the file is gone."""
    if not pdf_path:
        return None
    try:
        return Path(pdf_path).read_bytes()
    except OSError:
        return None


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


def _get_or_create_keyword(session: Session, name: str) -> Keyword:
    keyword = session.scalar(select(Keyword).where(Keyword.name == name))
    if keyword is None:
        keyword = Keyword(name=name)
        session.add(keyword)
        session.flush()
    return keyword


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
        _link_keyword(session, article, subject)


def _link_keyword(session: Session, article: Article, name: str) -> None:
    name = name.strip()
    if not name:
        return
    keyword = _get_or_create_keyword(session, name)
    exists = session.scalar(
        select(ArticleKeyword).where(
            ArticleKeyword.article_id == article.id,
            ArticleKeyword.keyword_id == keyword.id,
        )
    )
    if not exists:
        session.add(ArticleKeyword(article_id=article.id, keyword_id=keyword.id))


def apply_semantic_scholar(session: Session, article: Article, data: dict[str, Any]) -> None:
    if not article.abstract and data.get("abstract"):
        article.abstract = data["abstract"]
    for study_field in data.get("fieldsOfStudy") or []:
        _link_keyword(session, article, study_field)


def add_pdf_keywords(session: Session, article: Article, pdf_bytes: bytes) -> list[str]:
    """Extract author keywords from an uploaded PDF and link them to the article."""
    keywords = extract_keywords_from_pdf(pdf_bytes)
    for keyword in keywords:
        _link_keyword(session, article, keyword)
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
    pdf_bytes = read_stored_pdf(article.pdf_path)
    if pdf_bytes is None:
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
        pdf_bytes = read_stored_pdf(article.pdf_path)
        if pdf_bytes is None:
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


def backfill_missing_pdf_affiliations(session: Session) -> int:
    """Re-parse stored PDFs for author links still missing an affiliation.

    Complements `repair_shared_pdf_affiliations`, which only rewrites articles
    where everyone shares one joined value. Rows left NULL by an earlier parse —
    e.g. a name the header spelled with a detached accent — are retried here, so
    parser improvements reach an existing library without re-ingesting anything.
    Articles whose header genuinely yields nothing are re-read on each startup;
    that is a handful of first-page parses, not a fetch.
    """
    filled = 0
    articles = session.scalars(
        select(Article)
        .join(ArticleAuthor, ArticleAuthor.article_id == Article.id)
        .where(Article.pdf_path.is_not(None), ArticleAuthor.affiliation.is_(None))
        .distinct()
    ).all()
    for article in articles:
        filled += apply_pdf_affiliations(session, article)
    return filled


def _backfill_abstract(article: Article, pdf_bytes: bytes) -> None:
    """Fill a missing abstract from the PDF; never override a real one from Crossref/S2."""
    if article.abstract:
        return
    abstract = extract_abstract_from_pdf(pdf_bytes)
    if abstract:
        article.abstract = abstract


def backfill_pdf_header(session: Session, article: Article, pdf_bytes: bytes) -> None:
    """Fill a missing title, journal and author list from the PDF's front page.

    All three are read from the page's typography (see `extract_title_from_pdf`),
    which is the only source for a paper Crossref has no record of — a TeX-produced
    PDF typically leaves the metadata title empty, so such uploads land titleless
    and with nobody credited.

    Authors are only created for a record with no DOI and no stored Crossref
    message. Where one exists it owns the author list and its order, and
    `apply_crossref` rebuilds the links from scratch on every fetch.
    """
    if not article.title:
        title = extract_title_from_pdf(pdf_bytes)
        if title:
            article.title = title

    if not article.journal:
        journal = extract_journal_from_pdf(pdf_bytes)
        if journal:
            article.journal = journal

    if article.doi or article.crossref_json or article.author_links:
        return
    seen: set[int] = set()
    position = 0
    for name in extract_authors_from_pdf(pdf_bytes):
        author = _get_or_create_author(session, name, None)
        if author.id in seen:
            continue
        seen.add(author.id)
        session.add(
            ArticleAuthor(
                article_id=article.id, author_id=author.id, position=position
            )
        )
        position += 1


def attach_pdf(session: Session, article: Article, pdf_bytes: bytes) -> None:
    """Store a PDF for an article: file on disk, full text for search, keywords as topics."""
    article.pdf_path = save_pdf(pdf_bytes, article.doi)
    article.pdf_sha256 = pdf_sha256(pdf_bytes)
    article.pdf_text = extract_pdf_text(pdf_bytes)
    _backfill_abstract(article, pdf_bytes)
    backfill_pdf_header(session, article, pdf_bytes)
    session.commit()
    add_pdf_keywords(session, article, pdf_bytes)
    apply_pdf_affiliations(session, article)


def rescan_article_pdf(session: Session, article: Article) -> list[str]:
    """Re-run the scrub on an already-stored PDF: refresh full text, link keywords."""
    pdf_bytes = read_stored_pdf(article.pdf_path)
    if pdf_bytes is None:
        return []
    article.pdf_text = extract_pdf_text(pdf_bytes)
    _backfill_abstract(article, pdf_bytes)
    backfill_pdf_header(session, article, pdf_bytes)
    session.commit()
    keywords = add_pdf_keywords(session, article, pdf_bytes)
    apply_pdf_affiliations(session, article)
    return keywords


@dataclass
class PdfReplacement:
    """What swapping a new PDF into an existing record re-derived."""

    keywords: list[str] = field(default_factory=list)
    abstract_updated: bool = False
    affiliations_filled: int = 0
    doi_adopted: str | None = None
    conflicting_article_id: int | None = None


def replace_pdf(session: Session, article: Article, pdf_bytes: bytes) -> PdfReplacement:
    """Store a new PDF for `article` and re-parse the metadata the old one gave.

    Only what the superseded file wrote is rewritten: an abstract or affiliation
    that came from Crossref survives, while one parsed out of the replaced PDF is
    re-derived from the new file. That provenance is worked out by re-parsing the
    outgoing PDF and comparing — the columns don't record where a value came from.

    Keywords are only ever added. They are curated into topics by hand, so dropping
    the ones the old file contributed would silently unpick that classification.

    A record with no DOI adopts one found in the new PDF, unless another article
    already holds it (reported back instead, since the column is UNIQUE). The
    caller is responsible for queueing `process_article` when that happens.
    """
    result = PdfReplacement()
    previous_path = article.pdf_path
    links = session.scalars(
        select(ArticleAuthor)
        .where(ArticleAuthor.article_id == article.id)
        .order_by(ArticleAuthor.position)
    ).all()

    old_bytes = read_stored_pdf(previous_path)
    old_abstract = extract_abstract_from_pdf(old_bytes) if old_bytes else None
    old_affiliations: list[str | None] = (
        extract_author_affiliations(old_bytes, [link.author.full_name for link in links])
        if old_bytes and links
        else [None] * len(links)
    )

    if article.doi is None:
        found = extract_doi_from_pdf(pdf_bytes)
        if found:
            owner_id = session.scalar(select(Article.id).where(Article.doi == found))
            if owner_id is not None:
                result.conflicting_article_id = owner_id
            else:
                article.doi = found
                article.status = "pending"  # the caller's fetch flips this to ready
                result.doi_adopted = found

    # Written after the DOI is settled: the filename is derived from it.
    article.pdf_path = save_pdf(pdf_bytes, article.doi)
    article.pdf_sha256 = pdf_sha256(pdf_bytes)
    article.pdf_text = extract_pdf_text(pdf_bytes)

    if not article.abstract or article.abstract == old_abstract:
        abstract = extract_abstract_from_pdf(pdf_bytes)
        if abstract and abstract != article.abstract:
            article.abstract = abstract
            result.abstract_updated = True

    # Clear the affiliations the outgoing PDF supplied so the re-parse below fills
    # them from the new one; anything Crossref set is left alone.
    for link, previous in zip(links, old_affiliations, strict=False):
        if previous and link.affiliation == previous:
            link.affiliation = None
    backfill_pdf_header(session, article, pdf_bytes)
    session.commit()

    if previous_path and previous_path != article.pdf_path:
        Path(previous_path).unlink(missing_ok=True)  # superseded, nothing points at it

    result.keywords = add_pdf_keywords(session, article, pdf_bytes)
    result.affiliations_filled = apply_pdf_affiliations(session, article)
    return result


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
