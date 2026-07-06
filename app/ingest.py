"""Ingestion pipeline: DOI extraction/normalization, metadata APIs, background processing.

External API calls are isolated in small module-level functions (fetch_crossref,
fetch_semantic_scholar, fetch_unpaywall, download_pdf) so tests can monkeypatch them.
"""

import html
import json
import os
import re
import uuid
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


def _get_or_create_author(session: Session, full_name: str, orcid: str | None) -> Author:
    """ORCID match first; otherwise exact normalized-name match. No fuzzy merging (v1)."""
    if orcid:
        author = session.scalar(select(Author).where(Author.orcid == orcid))
        if author:
            return author
    author = session.scalar(
        select(Author).where(Author.full_name == full_name, Author.orcid.is_(None))
    )
    if author:
        if orcid:
            author.orcid = orcid
        return author
    author = Author(full_name=full_name, orcid=orcid)
    session.add(author)
    session.flush()
    return author


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
        session.add(ArticleAuthor(article_id=article.id, author_id=author.id, position=position))
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
                        article.pdf_path = save_pdf(pdf_bytes, article.doi)
        except Exception:
            pass

        article.status = "ready"
        session.commit()
