import re
import unicodedata
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import bibtex, contacts, db, ingest, word_xml
from app.models import Article, ArticleAuthor, ArticleTopic, Author, Topic

app = FastAPI(title="RefBase")

_BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=_BASE_DIR / "templates")
app.mount("/static", StaticFiles(directory=_BASE_DIR / "static"), name="static")


def _short_author_name(full_name: str) -> str:
    """'Michał Spirzewski' -> 'M. Spirzewski'; single-token names unchanged."""
    parts = full_name.split()
    if len(parts) < 2:
        return full_name
    return f"{parts[0][0]}. {parts[-1]}"


templates.env.filters["short_name"] = _short_author_name


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    # Make the hard-coded alias table retroactive: authors stored before an alias
    # was added would otherwise stay split forever.
    with db.SessionLocal() as session:
        ingest.apply_name_aliases(session)
        # Same idea for the date columns: derive them from Crossref JSON already on
        # disk, so an existing library gains them without re-fetching anything.
        ingest.backfill_dates(session)


def _affiliation_key(text: str) -> str:
    """Normalize an affiliation for de-dup: strip case, diacritics, and punctuation."""
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", ascii_only.lower()).strip()


# Any affiliation mentioning NCNR is folded into this single canonical entry,
# regardless of the street address / spelling variants across PDFs.
_NCNR_PHRASE = "national centre for nuclear research"
_NCNR_LABEL = "National Centre for Nuclear Research"


def _canonical_affiliation(text: str) -> tuple[str, str]:
    """Return a (dedup-key, display-text) pair, grouping NCNR variants into one."""
    key = _affiliation_key(text)
    if _NCNR_PHRASE in key:
        return _NCNR_PHRASE, _NCNR_LABEL
    return key, text


def _clean(value: str | None) -> str | None:
    """HTML forms send empty strings for blank fields; treat them as absent."""
    if value is None:
        return None
    value = value.strip()
    return value or None


def _wants_html(request: Request) -> bool:
    """Browser form posts accept text/html; API clients get JSON."""
    return "text/html" in request.headers.get("accept", "")


# --------------------------------------------------------------------------- ingest API

@app.post("/api/ingest")
async def ingest_endpoint(
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(db.get_db),
    file: UploadFile | None = File(None),
    url: str | None = Form(None),
    doi: str | None = Form(None),
    title: str | None = Form(None),
    added_by: str | None = Form(None),
):
    url, doi, title, added_by = _clean(url), _clean(doi), _clean(title), _clean(added_by)

    pdf_bytes: bytes | None = None
    if file is not None and file.filename:
        pdf_bytes = await file.read()

    if not (pdf_bytes or url or doi or title):
        message = "Nothing to add — provide a PDF, a URL, or a DOI."
        if _wants_html(request):  # re-render the form with the error + a return button
            return templates.TemplateResponse(
                request, "upload.html", {"error": message}, status_code=422
            )
        raise HTTPException(status_code=422, detail=message)

    # Resolve DOI: raw input -> URL -> PDF text/metadata.
    resolved = ingest.extract_doi(doi) or ingest.extract_doi(url)
    if not resolved and pdf_bytes:
        resolved = ingest.extract_doi_from_pdf(pdf_bytes)

    if resolved:
        existing = session.scalar(select(Article).where(Article.doi == resolved))
        if existing:
            result = _duplicate_response(session, background_tasks, existing, pdf_bytes)
            if _wants_html(request):
                return RedirectResponse(
                    f"{result['url']}?existing=1&pdf_attached={int(result['pdf_attached'])}",
                    status_code=303,
                )
            return result

        article = Article(
            doi=resolved, title=title, source_url=url, added_by=added_by, status="pending"
        )
        session.add(article)
        try:
            session.commit()
        except IntegrityError:  # lost a race on the UNIQUE(doi) constraint
            session.rollback()
            existing = session.scalar(select(Article).where(Article.doi == resolved))
            result = _duplicate_response(session, background_tasks, existing, pdf_bytes)
            if _wants_html(request):
                return RedirectResponse(
                    f"{result['url']}?existing=1&pdf_attached={int(result['pdf_attached'])}",
                    status_code=303,
                )
            return result

        if pdf_bytes:
            ingest.attach_pdf(session, article, pdf_bytes)
        background_tasks.add_task(ingest.process_article, article.id)
        if _wants_html(request):
            return RedirectResponse(f"/articles/{article.id}?added=1", status_code=303)
        return {"status": "created", "article_id": article.id, "doi": resolved,
                "processing": True, "warnings": []}

    # No DOI anywhere: create a stub from PDF metadata / user input.
    stub_title = title or (ingest.pdf_title(pdf_bytes) if pdf_bytes else None)
    warnings = []
    if stub_title:
        similar = ingest.find_similar_titles(session, stub_title)
        if similar:
            warnings.append({"type": "possible_duplicate", "matches": similar})

    article = Article(
        title=stub_title, source_url=url, added_by=added_by, status="ready"
    )
    session.add(article)
    session.commit()
    if pdf_bytes:
        ingest.attach_pdf(session, article, pdf_bytes)
    if _wants_html(request):
        suffix = "&check_dup=1" if warnings else ""
        return RedirectResponse(f"/articles/{article.id}?added=1{suffix}", status_code=303)
    return {"status": "created", "article_id": article.id, "doi": None,
            "processing": False, "warnings": warnings}


def _duplicate_response(
    session: Session,
    background_tasks: BackgroundTasks,
    existing: Article,
    pdf_bytes: bytes | None,
):
    pdf_attached = False
    if pdf_bytes and not existing.pdf_path:
        ingest.attach_pdf(session, existing, pdf_bytes)
        pdf_attached = True
    if existing.status == "metadata_failed" and not existing.crossref_json:
        background_tasks.add_task(ingest.process_article, existing.id)
    return {
        "status": "already_exists",
        "article_id": existing.id,
        "doi": existing.doi,
        "pdf_attached": pdf_attached,
        "detail": "Article is already in the library.",
        "url": f"/articles/{existing.id}",
    }


@app.post("/articles/{article_id}/rescan")
def rescan_article(
    request: Request, article_id: int, session: Session = Depends(db.get_db)
):
    article = session.get(Article, article_id)
    if article is None or article.hidden:
        raise HTTPException(status_code=404, detail="Article not found")
    keywords = ingest.rescan_article_pdf(session, article)
    if _wants_html(request):
        return RedirectResponse(
            f"/articles/{article_id}?rescanned={len(keywords)}", status_code=303
        )
    return {"article_id": article_id, "keywords": keywords}


@app.post("/api/rescan-pdfs")
def rescan_all_pdfs(session: Session = Depends(db.get_db)):
    """Re-run the keyword scrub over every stored PDF in the library."""
    articles = session.scalars(
        select(Article).where(Article.pdf_path.is_not(None), Article.hidden.is_(False))
    ).all()
    found = {}
    for article in articles:
        keywords = ingest.rescan_article_pdf(session, article)
        if keywords:
            found[article.id] = keywords
    return {"pdfs_scanned": len(articles), "articles_with_keywords": found}


@app.get("/api/articles/{article_id}")
def article_status(article_id: int, session: Session = Depends(db.get_db)):
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return {
        "id": article.id,
        "doi": article.doi,
        "title": article.title,
        "year": article.year,
        "journal": article.journal,
        "status": article.status,
        "has_pdf": article.pdf_path is not None,
        "authors": [a.full_name for a in article.authors],
        "topics": [t.name for t in article.topics],
    }


# --------------------------------------------------------------------------- pages (minimal until M2)

# Clickable-header sort keys. "year" is the default (newest publication first, which
# is how researchers read a bibliography); "recent" is date *added* to the library.
# "author" needs the author relationship so it's sorted in Python, the rest in SQL.
_SORT_COLUMNS = {
    "recent": Article.created_at,
    "title": Article.title,
    # Sorts on the ISO issue date, so papers sharing a year order by month/day.
    # The key stays "year" to keep existing ?sort=year links working.
    "year": Article.published_date,
    "online": Article.online_date,
    "journal": Article.journal,
    "status": Article.status,
}
_SORT_KEYS = set(_SORT_COLUMNS) | {"author"}


@app.get("/")
def index(
    request: Request,
    session: Session = Depends(db.get_db),
    sort: str = "year",
    order: str = "",
    topic: int | None = None,
):
    if sort not in _SORT_KEYS:
        sort = "year"
    # Sensible default direction per column: newest-first for date/year, A→Z otherwise.
    if order not in ("asc", "desc"):
        order = "desc" if sort in ("recent", "year", "online") else "asc"
    descending = order == "desc"

    active_topic = session.get(Topic, topic) if topic is not None else None

    query = (
        select(Article)
        .where(Article.hidden.is_(False))
        .options(
            selectinload(Article.author_links).selectinload(ArticleAuthor.author),
            selectinload(Article.topics),
        )
    )
    if active_topic is not None:  # filter to papers carrying this keyword/topic
        query = query.join(ArticleTopic, ArticleTopic.article_id == Article.id).where(
            ArticleTopic.topic_id == active_topic.id
        )
    column = _SORT_COLUMNS.get(sort, Article.year)
    # Papers share a year (or a journal) constantly, so break ties on date added
    # instead of letting SQLite return them in whatever order it likes.
    query = query.order_by(
        column.desc() if descending else column.asc(), Article.created_at.desc()
    )
    # Show the whole tagged set when filtering; otherwise the most recent window.
    articles = list(session.scalars(query if active_topic is not None else query.limit(20)))

    if sort == "author":
        def first_author_surname(article: Article) -> tuple[int, str]:
            if not article.author_links:
                return (1, "")  # articles without authors sort last
            tokens = article.author_links[0].author.full_name.split()
            return (0, tokens[-1].lower() if tokens else "")

        articles.sort(key=first_author_surname, reverse=descending)

    # Unique authors in the library + their (non-hidden) publication counts.
    pub_count = func.count(ArticleAuthor.article_id)
    author_stats = session.execute(
        select(Author, pub_count)
        .join(ArticleAuthor, ArticleAuthor.author_id == Author.id)
        .join(Article, Article.id == ArticleAuthor.article_id)
        .where(Article.hidden.is_(False), Author.merged_into_id.is_(None))
        .group_by(Author.id)
        .order_by(pub_count.desc(), Author.full_name)
    ).all()

    # Authors with at least one NCNR affiliation, to split the panel into groups.
    ncnr_ids = {
        row[0]
        for row in session.execute(
            select(ArticleAuthor.author_id)
            .distinct()
            .where(ArticleAuthor.affiliation.ilike(f"%{_NCNR_LABEL}%"))
        )
    }
    ncnr_authors = [pair for pair in author_stats if pair[0].id in ncnr_ids]
    other_authors = [pair for pair in author_stats if pair[0].id not in ncnr_ids]

    # Keywords/topics across the library + how many articles carry each.
    topic_count = func.count(ArticleTopic.article_id)
    topic_stats = session.execute(
        select(Topic, topic_count)
        .join(ArticleTopic, ArticleTopic.topic_id == Topic.id)
        .join(Article, Article.id == ArticleTopic.article_id)
        .where(Article.hidden.is_(False))
        .group_by(Topic.id)
        .order_by(topic_count.desc(), Topic.name)
    ).all()

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "articles": articles,
            "sort": sort,
            "order": order,
            "author_total": len(author_stats),
            "ncnr_authors": ncnr_authors,
            "other_authors": other_authors,
            "ncnr_label": _NCNR_LABEL,
            "topic_stats": topic_stats,
            "active_topic": active_topic,
        },
    )


@app.get("/upload")
def upload_page(request: Request):
    return templates.TemplateResponse(request, "upload.html", {})


@app.get("/search")
def search_page(request: Request, session: Session = Depends(db.get_db), q: str = ""):
    results = []
    if q.strip():
        for article_id, snippet in db.search_articles(session, q):
            article = session.get(Article, article_id)
            if article and not article.hidden:
                results.append({"article": article, "snippet": snippet})
    return templates.TemplateResponse(
        request, "search.html", {"q": q, "results": results}
    )


# Same default as the dashboard: a publication list reads by year, not by upload date.
# Every key breaks ties on date added so equal years keep a stable order.
_PUB_SORT_KEYS = {
    "recent": lambda a: (a.created_at,),
    "title": lambda a: ((a.title or "").lower(), a.created_at),
    # Zero-padded ISO strings, so plain string ordering is chronological. Undated
    # papers get "" and land first ascending / last descending, like NULL in SQL.
    "year": lambda a: (a.published_date or "", a.created_at),
    "online": lambda a: (a.online_date or "", a.created_at),
    "journal": lambda a: ((a.journal or "").lower(), a.created_at),
}


@app.get("/authors/{author_id}")
def author_detail(
    request: Request,
    author_id: int,
    session: Session = Depends(db.get_db),
    saved: int = 0,
    sort: str = "year",
    order: str = "",
    topic: int | None = None,
):
    author = session.get(Author, author_id)
    if author is None:
        raise HTTPException(status_code=404, detail="Author not found")
    if author.merged_into_id is not None:  # this row was folded into a canonical author
        canonical = ingest.canonical_author(session, author)
        return RedirectResponse(f"/authors/{canonical.id}", status_code=301)
    articles = list(
        session.scalars(
            select(Article)
            .join(ArticleAuthor, ArticleAuthor.article_id == Article.id)
            .where(ArticleAuthor.author_id == author_id, Article.hidden.is_(False))
            .options(
                selectinload(Article.author_links).selectinload(ArticleAuthor.author),
                selectinload(Article.topics),
            )
            .order_by(Article.created_at.desc())
        )
    )
    co_authors: dict[int, Author] = {}
    joint_counts: dict[int, int] = {}
    topics = {}
    topic_counts: dict[int, int] = {}
    # Ordered de-dup of this author's affiliations, keyed so near-identical strings
    # (same text bar case/diacritics/punctuation — PDF encoding noise) collapse.
    affiliations: dict[str, str] = {}
    is_ncbj = False  # affiliated with National Centre for Nuclear Research (NCBJ)
    for article in articles:
        for link in article.author_links:
            if link.author_id != author_id:
                co_authors[link.author_id] = link.author
                joint_counts[link.author_id] = joint_counts.get(link.author_id, 0) + 1
            elif link.affiliation:
                for aff in link.affiliation.split("; "):
                    aff = aff.strip()
                    if aff:
                        key, display = _canonical_affiliation(aff)
                        affiliations.setdefault(key, display)
                        if key == _NCNR_PHRASE:
                            is_ncbj = True
        for art_topic in article.topics:
            topics[art_topic.id] = art_topic
            topic_counts[art_topic.id] = topic_counts.get(art_topic.id, 0) + 1
    # Most frequent collaborators first, then alphabetical.
    co_author_stats = sorted(
        ((co_authors[cid], joint_counts[cid]) for cid in co_authors),
        key=lambda pair: (-pair[1], pair[0].full_name),
    )

    # Optional keyword/topic filter — narrows only the publications table below,
    # leaving the co-author / affiliation / topic summaries computed over all papers.
    active_topic = topics.get(topic) if topic is not None else None
    if active_topic is not None:
        articles = [a for a in articles if any(t.id == active_topic.id for t in a.topics)]

    # Sortable publications table (clickable headers). Default: newest published first.
    if sort not in _PUB_SORT_KEYS:
        sort = "year"
    if order not in ("asc", "desc"):
        order = "desc" if sort in ("recent", "year", "online") else "asc"
    articles = sorted(articles, key=_PUB_SORT_KEYS[sort], reverse=(order == "desc"))

    return templates.TemplateResponse(
        request,
        "author.html",
        {
            "author": author,
            "articles": articles,
            "sort": sort,
            "order": order,
            "co_author_stats": co_author_stats,
            "affiliations": list(affiliations.values()),
            # (topic, count) pairs — most-used keyword first, then alphabetical.
            "topic_stats": sorted(
                ((topics[tid], topic_counts[tid]) for tid in topics),
                key=lambda pair: (-pair[1], pair[0].name),
            ),
            "active_topic": active_topic,
            "is_ncbj": is_ncbj,
            "contact": contacts.get(author.id) if is_ncbj else {},
            "contact_saved": bool(saved),
        },
    )


@app.post("/authors/{author_id}/contact")
def save_author_contact(
    author_id: int,
    session: Session = Depends(db.get_db),
    phone: str | None = Form(None),
    meeting_link: str | None = Form(None),
):
    """Add/update NCBJ auxiliary contact data (persisted to the JSON file)."""
    author = session.get(Author, author_id)
    if author is None:
        raise HTTPException(status_code=404, detail="Author not found")
    author = ingest.canonical_author(session, author)  # store against the canonical row
    contacts.save(
        author.id,
        author.full_name,
        {"phone": phone, "meeting_link": meeting_link},
    )
    return RedirectResponse(f"/authors/{author.id}?saved=1", status_code=303)


@app.get("/articles/{article_id}")
def article_detail(
    request: Request,
    article_id: int,
    session: Session = Depends(db.get_db),
    added: int = 0,
    existing: int = 0,
    pdf_attached: int = 0,
    check_dup: int = 0,
    rescanned: int = -1,
):
    article = session.get(Article, article_id)
    if article is None or article.hidden:
        raise HTTPException(status_code=404, detail="Article not found")
    similar = []
    if check_dup and article.title:
        similar = [
            match
            for match in ingest.find_similar_titles(session, article.title)
            if match["article_id"] != article.id
        ]
    return templates.TemplateResponse(
        request,
        "article.html",
        {
            "article": article,
            "added": added,
            "existing": existing,
            "pdf_attached": pdf_attached,
            "similar": similar,
            "rescanned": rescanned,
        },
    )


@app.get("/articles/{article_id}/bibtex", response_class=PlainTextResponse)
def article_bibtex(article_id: int, session: Session = Depends(db.get_db)):
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return PlainTextResponse(
        bibtex.article_bibtex(article),
        media_type="application/x-bibtex",
        headers={"Content-Disposition": f'attachment; filename="article-{article_id}.bib"'},
    )


@app.get("/articles/{article_id}/word-xml", response_class=PlainTextResponse)
def article_word_xml(article_id: int, session: Session = Depends(db.get_db)):
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return PlainTextResponse(
        word_xml.article_word_xml(article),
        media_type="application/xml",
        headers={
            "Content-Disposition": f'attachment; filename="{word_xml.xml_filename(article)}"'
        },
    )


def _articles_for_export(session: Session, ids: list[int]) -> list[Article]:
    """Non-hidden articles for the selected ids, kept in the order they were ticked."""
    if not ids:
        raise HTTPException(status_code=400, detail="No articles selected")
    found = {
        article.id: article
        for article in session.scalars(
            select(Article)
            .where(Article.id.in_(ids), Article.hidden.is_(False))
            .options(selectinload(Article.author_links).selectinload(ArticleAuthor.author))
        )
    }
    articles = [found[i] for i in dict.fromkeys(ids) if i in found]
    if not articles:
        raise HTTPException(status_code=404, detail="None of the selected articles exist")
    return articles


@app.post("/export/bibtex", response_class=PlainTextResponse)
def export_bibtex(
    ids: list[int] = Form(default=[]), session: Session = Depends(db.get_db)
):
    """Merge the selected articles into one .bib (cite keys deduplicated across it)."""
    articles = _articles_for_export(session, ids)
    return PlainTextResponse(
        bibtex.export_bibtex(articles),
        media_type="application/x-bibtex",
        headers={
            "Content-Disposition": f'attachment; filename="refbase-{len(articles)}-refs.bib"'
        },
    )


@app.post("/export/word-xml", response_class=PlainTextResponse)
def export_word_xml(
    ids: list[int] = Form(default=[]), session: Session = Depends(db.get_db)
):
    """Same selection as the .bib export, as one Word Source Manager document."""
    articles = _articles_for_export(session, ids)
    return PlainTextResponse(
        word_xml.export_word_xml(articles),
        media_type="application/xml",
        headers={
            "Content-Disposition": f'attachment; filename="refbase-{len(articles)}-refs.xml"'
        },
    )


@app.get("/articles/{article_id}/pdf")
def article_pdf(article_id: int, session: Session = Depends(db.get_db), download: int = 0):
    article = session.get(Article, article_id)
    if article is None or not article.pdf_path:
        raise HTTPException(status_code=404, detail="No PDF for this article")
    return FileResponse(
        article.pdf_path,
        media_type="application/pdf",
        filename=bibtex.pdf_filename(article),
        content_disposition_type="attachment" if download else "inline",
    )
