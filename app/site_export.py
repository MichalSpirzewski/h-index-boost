"""Selected-publication summaries for offline archives and hosted share links.

The archive form holds one `summary.html` and the available selected PDFs, all at
the same level. The hosted form uses the same rendering with live RefBase PDF
links. CSS and JavaScript are embedded in both.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from app import bibtex
from app.affiliations import NCNR_LABEL, canonical_affiliation
from app.models import Article, Author, Topic
from app.templating import templates

_STATIC_DIR = Path(__file__).parent / "static"

SUMMARY_NAME = "summary.html"


def _row(article: Article, key: str, entry: str) -> dict:
    """Everything the exported page needs about one article."""
    pdf_path = Path(article.pdf_path) if article.pdf_path else None
    has_pdf = pdf_path is not None and pdf_path.is_file()
    # Free-text haystack for the page's search box, matching what the server-side
    # FTS index covers (title, abstract, journal) plus authors, topics and the DOI.
    haystack = " ".join(
        part
        for part in (
            article.title,
            article.abstract,
            article.journal,
            article.doi,
            " ".join(author.full_name for author in article.authors),
            " ".join(topic.name for topic in article.topics),
        )
        if part
    )
    return {
        "article": article,
        "key": key,
        "bibtex": entry,
        "pdf_path": pdf_path if has_pdf else None,
        # The PDFs sit beside summary.html in the extracted folder.
        "pdf": f"{key}.pdf" if has_pdf else None,
        "search": haystack.casefold(),
        "affiliations": _affiliations(article),
    }


def _affiliations(article: Article) -> dict[int, list[str]]:
    """This paper's affiliations per author id, de-duplicated like the author page."""
    per_author: dict[int, list[str]] = {}
    for link in article.author_links:
        seen: dict[str, str] = {}
        for part in (link.affiliation or "").split("; "):
            part = part.strip()
            if part:
                key, display = canonical_affiliation(part)
                seen.setdefault(key, display)
        per_author[link.author_id] = list(seen.values())
    return per_author


def _rows(articles: list[Article]) -> list[dict]:
    """Per-article export data. Cite keys are deduplicated across the whole
    selection, so they are also unique as PDF filenames inside the archive."""
    entries = bibtex.export_entries(articles)
    return [
        _row(article, key, entry)
        for article, (key, entry) in zip(articles, entries, strict=True)
    ]


def _author_stats(rows: list[dict]) -> tuple[list, list]:
    """(NCNR authors, other authors) as (author, count) pairs, most papers first —
    the same split the dashboard's Authors panel shows, over this selection only."""
    authors: dict[int, Author] = {}
    counts: dict[int, int] = {}
    ncnr: set[int] = set()
    for row in rows:
        for author in row["article"].authors:
            authors[author.id] = author
            counts[author.id] = counts.get(author.id, 0) + 1
        for author_id, affiliations in row["affiliations"].items():
            if NCNR_LABEL in affiliations:
                ncnr.add(author_id)
    stats = sorted(
        ((authors[i], counts[i]) for i in authors),
        key=lambda pair: (-pair[1], pair[0].full_name),
    )
    return (
        [pair for pair in stats if pair[0].id in ncnr],
        [pair for pair in stats if pair[0].id not in ncnr],
    )


def _topic_stats(rows: list[dict]) -> list[tuple[Topic, int]]:
    topics: dict[int, Topic] = {}
    counts: dict[int, int] = {}
    for row in rows:
        for topic in row["article"].topics:
            topics[topic.id] = topic
            counts[topic.id] = counts.get(topic.id, 0) + 1
    return sorted(
        ((topics[i], counts[i]) for i in topics),
        key=lambda pair: (-pair[1], pair[0].name),
    )


def _render_summary(
    rows: list[dict],
    *,
    generated: str,
    hosted: bool = False,
    share_url: str | None = None,
    link_created: bool = False,
) -> str:
    """Render the common offline/hosted selected-publications page."""
    ncnr_authors, other_authors = _author_stats(rows)
    return templates.env.get_template("export_site.html").render(
        rows=rows,
        # The downloadable dashboard intentionally mirrors its two dashboard
        # sections. A hosted share shows every selected paper exactly once.
        cited_rows=(
            [] if hosted else [row for row in rows if row["article"].cite_first]
        ),
        ncnr_authors=ncnr_authors,
        other_authors=other_authors,
        author_total=len(ncnr_authors) + len(other_authors),
        topic_stats=_topic_stats(rows),
        ncnr_label=NCNR_LABEL,
        style_css=(_STATIC_DIR / "style.css").read_text(encoding="utf-8"),
        offline_css=(_STATIC_DIR / "offline.css").read_text(encoding="utf-8"),
        offline_js=(_STATIC_DIR / "offline.js").read_text(encoding="utf-8"),
        export_js=(_STATIC_DIR / "export.js").read_text(encoding="utf-8"),
        generated=generated,
        hosted=hosted,
        share_url=share_url,
        link_created=link_created,
    )


def render_page(rows: list[dict]) -> str:
    """Render the archive's fully self-contained summary.html."""
    return _render_summary(
        rows, generated=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    )


def render_shared_page(
    articles: list[Article],
    *,
    share_url: str,
    created_at: datetime,
    link_created: bool = False,
) -> str:
    """Render a live RefBase page limited to one persisted selection."""
    rows = _rows(articles)
    for row in rows:
        if row["pdf"] is not None:
            row["pdf"] = f"/articles/{row['article'].id}/pdf"
    return _render_summary(
        rows,
        generated=created_at.strftime("%Y-%m-%d %H:%M UTC"),
        hosted=True,
        share_url=share_url,
        link_created=link_created,
    )


def build_archive(articles: list[Article]) -> bytes:
    """ZIP summary.html and the available selected PDFs in one flat folder."""
    rows = _rows(articles)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(SUMMARY_NAME, render_page(rows))
        for row in rows:
            if row["pdf_path"] is not None:
                archive.write(row["pdf_path"], arcname=row["pdf"])
    return output.getvalue()
