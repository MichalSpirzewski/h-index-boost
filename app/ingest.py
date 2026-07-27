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
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
import httpx
from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import contacts, db
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

_KEYWORD_LINE_RE = re.compile(
    r"^[ \t]*key\s*words?\s*[:\-—–][ \t]*(.+)$", re.IGNORECASE | re.MULTILINE
)
_MAX_KEYWORDS = 10
_MAX_KEYWORD_LEN = 60


def split_keywords(raw: str) -> list[str]:
    """Split an author-keyword string on common separators; dedupe case-insensitively."""
    keywords: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[;,·•]", raw):
        keyword = part.strip().rstrip(".").strip()
        if not keyword or len(keyword) > _MAX_KEYWORD_LEN:
            continue
        if keyword.lower() in seen:
            continue
        seen.add(keyword.lower())
        keywords.append(keyword)
    return keywords[:_MAX_KEYWORDS]


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
            match = _KEYWORD_LINE_RE.search(page.get_text())
            if match:
                keywords = split_keywords(match.group(1))
                if keywords:
                    return keywords
    return []


# --------------------------------------------------------------------------- affiliations

# Institution words that mark a line as an affiliation rather than an author list.
# Stems (no trailing \b) so "Universi" also matches University/Universität/Università.
_AFF_KEYWORD_RE = re.compile(
    r"\b(Universi|Univerz|Institut|Instytut|Centre|Center|Centrum|Department"
    r"|Laborator|Faculty|Fakult|School|College|Academ|Politech|Politecnico"
    r"|Ministr|Hospital|Wydział|Division|GmbH|Research)",
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
    for i, line in enumerate(lines[:40]):
        s = line.strip()
        if not s:
            continue
        if ordered_affs and _AFF_BOUNDARY_RE.match(s):
            break
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
            ordered_affs.append(_clean_affiliation(s))
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


# --------------------------------------------------------------------------- emails

NCBJ_EMAIL_DOMAIN = "@ncbj.gov.pl"
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


# Latin letters that carry no NFKD decomposition, so diacritic-stripping misses them.
_TRANSLIT = str.maketrans({"ł": "l", "ø": "o", "đ": "d", "ħ": "h", "ß": "ss"})


def _ascii_fold(text: str) -> str:
    """Lowercase and strip diacritics so 'Sierchuła' matches 'sierchula'."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.lower().translate(_TRANSLIT)


def extract_ncbj_emails(pdf_bytes: bytes) -> list[str]:
    """Unique @ncbj.gov.pl e-mails from the first two pages, in order of appearance."""
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            text = "\n".join(doc[i].get_text() for i in range(min(2, doc.page_count)))
    except Exception:
        return []
    seen: dict[str, None] = {}
    for match in _EMAIL_RE.findall(text):
        email = match.lower()
        if email.endswith(NCBJ_EMAIL_DOMAIN):
            seen.setdefault(email, None)
    return list(seen)


def correlate_emails_to_authors(
    emails: list[str], author_names: list[str]
) -> dict[str, str]:
    """Match NCBJ e-mails to authors via the 'firstname.lastname' local part.

    Returns {author_name: email}. Colliding surnames (e.g. two Skrzypeks) are
    disambiguated by the given-name token; genuinely ambiguous ones are skipped.
    """
    by_family: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for name in author_names:
        parts = name.split()
        if not parts:
            continue
        by_family[_ascii_fold(parts[-1])].append((name, _ascii_fold(parts[0])))

    result: dict[str, str] = {}
    for email in emails:
        tokens = [t for t in re.split(r"[._\-]+", email.split("@")[0]) if t]
        if not tokens:
            continue
        candidates = by_family.get(_ascii_fold(tokens[-1]))
        if not candidates:
            continue
        if len(candidates) == 1:
            chosen = candidates[0][0]
        else:  # same surname → disambiguate on the given-name token
            first_tok = _ascii_fold(tokens[0])
            chosen = next(
                (
                    name
                    for name, first in candidates
                    if first and (first.startswith(first_tok) or first_tok.startswith(first))
                ),
                None,
            )
        if chosen:
            result.setdefault(chosen, email)
    return result


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


# --------------------------------------------------------------------------- metadata mapping

def _strip_jats(abstract: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", abstract)).strip()


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


def _get_or_create_author(session: Session, full_name: str, orcid: str | None) -> Author:
    """Group an incoming author with an existing one when possible (v1, no fuzzy).

    Match order: ORCID, then exact normalized name (regardless of the existing
    row's ORCID). Any match resolves to its canonical author so contributions
    accrue to a single base record; a missing ORCID on the canonical is filled in.
    """
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

    # Preserve an ORCID that would otherwise be lost (UNIQUE: clear it off source first).
    if target.orcid is None and source.orcid:
        target.orcid, source.orcid = source.orcid, None

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


def apply_crossref(session: Session, article: Article, message: dict[str, Any]) -> None:
    article.crossref_json = json.dumps(message)
    titles = message.get("title") or []
    if titles:
        article.title = titles[0]
    containers = message.get("container-title") or []
    if containers:
        article.journal = containers[0]
    date_parts = (message.get("issued") or {}).get("date-parts") or [[]]
    if date_parts[0] and date_parts[0][0]:
        article.year = int(date_parts[0][0])
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


def apply_pdf_emails(session: Session, article: Article) -> int:
    """Correlate @ncbj.gov.pl e-mails in the PDF to authors and store them as contacts.

    Only fills an author's contact e-mail when it's currently empty, so a manually
    entered address is never clobbered. Returns the number of e-mails newly stored.
    """
    if not article.pdf_path:
        return 0
    links = session.scalars(
        select(ArticleAuthor)
        .where(ArticleAuthor.article_id == article.id)
        .order_by(ArticleAuthor.position)
    ).all()
    if not links:
        return 0
    try:
        pdf_bytes = Path(article.pdf_path).read_bytes()
    except OSError:
        return 0

    emails = extract_ncbj_emails(pdf_bytes)
    if not emails:
        return 0

    id_by_name = {session.get(Author, link.author_id).full_name: link.author_id for link in links}
    correlated = correlate_emails_to_authors(emails, list(id_by_name))
    filled = 0
    for name, email in correlated.items():
        author_id = id_by_name[name]
        existing = contacts.get(author_id)
        if existing.get("email"):
            continue  # keep whatever's already there (manual or previously derived)
        contacts.save(
            author_id,
            name,
            {
                "email": email,
                "phone": existing.get("phone"),
                "meeting_link": existing.get("meeting_link"),
            },
        )
        filled += 1
    return filled


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
    apply_pdf_emails(session, article)
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

        # Fill any author affiliations Crossref didn't provide from the PDF header,
        # and correlate NCBJ corresponding-author e-mails to their authors.
        try:
            apply_pdf_affiliations(session, article)
            apply_pdf_emails(session, article)
        except Exception:
            pass

        article.status = "ready"
        session.commit()
