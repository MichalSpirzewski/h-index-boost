import io
import mimetypes
import secrets
import zipfile
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import (
    api_v1,
    bibtex,
    contacts,
    db,
    ingest,
    project_document_parser,
    site_export,
    word_xml,
)
from app.affiliations import NCNR_LABEL, NCNR_PHRASE, canonical_affiliation
from app.models import (
    Article,
    ArticleAuthor,
    ArticleKeyword,
    Author,
    DeliverableDocument,
    JournalPublication,
    Keyword,
    MilestoneDocument,
    Project,
    ProjectDocument,
    SharedSelection,
    SharedSelectionArticle,
    Topic,
    TopicKeyword,
)
from app.templating import templates

app = FastAPI(title="RefBase")
app.include_router(api_v1.router)

_BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=_BASE_DIR / "static"), name="static")

_PROJECT_FILE_SUFFIXES = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".txt",
}
_MAX_PROJECT_FILE_SIZE = 50 * 1024 * 1024


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    # Make the hard-coded alias table retroactive: authors stored before an alias
    # was added would otherwise stay split forever.
    with db.SessionLocal() as session:
        ingest.apply_name_aliases(session)
        # Fold author rows left split by the ORCID-only matching used before the
        # name-match path existed; current ingest cannot create them any more.
        ingest.merge_duplicate_authors(session)
        # Fold forms such as "E. Skrzypek" into a unique full-name match while
        # leaving ambiguous initial + surname combinations untouched.
        ingest.merge_initial_authors(session)
        # Re-run affiliation mapping for legacy PDFs where separate marker lines
        # previously caused every institution to be assigned to every author.
        ingest.repair_shared_pdf_affiliations(session)
        # Retry author links the header parser left empty — accent handling has
        # improved since some rows were written, and the PDFs are already on disk.
        ingest.backfill_missing_pdf_affiliations(session)
        # Same idea for the date columns: derive them from Crossref JSON already on
        # disk, so an existing library gains them without re-fetching anything.
        ingest.backfill_dates(session)
        # Crossref occasionally supplies HTML entities in container titles. Keep
        # journal links and page identifiers human-readable for old and new rows.
        ingest.backfill_journal_names(session)
        # Fingerprint PDFs stored before the column existed, so the duplicate
        # guard covers the library that already exists. One pass, then never again.
        ingest.backfill_pdf_hashes(session)


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

        article = JournalPublication(
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
        return {
            "status": "created",
            "article_id": article.id,
            "publication_type": article.publication_type,
            "doi": resolved,
            "processing": True,
            "warnings": [],
        }

    # No DOI anywhere, so there is nothing unique to key on — except the file
    # itself. An identical PDF already in the library means this is the same paper
    # being added a second time, which is how a library collects three copies of
    # one DOI-less paper.
    if pdf_bytes:
        same_file = ingest.find_by_pdf_hash(session, pdf_bytes)
        if same_file is not None:
            result = _duplicate_response(
                session, background_tasks, same_file, pdf_bytes
            )
            if _wants_html(request):
                return RedirectResponse(f"{result['url']}?existing=1", status_code=303)
            return result

    # Create a stub from PDF metadata / user input.
    stub_title = title or (ingest.pdf_title(pdf_bytes) if pdf_bytes else None)
    warnings = []
    if stub_title:
        similar = ingest.find_similar_titles(session, stub_title)
        if similar:
            warnings.append({"type": "possible_duplicate", "matches": similar})

    article = JournalPublication(
        title=stub_title, source_url=url, added_by=added_by, status="ready"
    )
    session.add(article)
    session.commit()
    if pdf_bytes:
        ingest.attach_pdf(session, article, pdf_bytes)
    if _wants_html(request):
        suffix = "&check_dup=1" if warnings else ""
        return RedirectResponse(f"/articles/{article.id}?added=1{suffix}", status_code=303)
    return {
        "status": "created",
        "article_id": article.id,
        "publication_type": article.publication_type,
        "doi": None,
        "processing": False,
        "warnings": warnings,
    }


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
        "publication_type": existing.publication_type,
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


_MAX_ARTICLE_PDF_SIZE = 50 * 1024 * 1024
# Codes travel in the redirect query string; the page renders the message.
_PDF_UPLOAD_ERRORS = {
    "empty": "The selected file is empty — the stored PDF was left as it was.",
    "too_large": "The selected file is larger than 50 MB.",
    "not_pdf": "That file is not a PDF — the stored PDF was left as it was.",
}


def _pdf_upload_error(content: bytes) -> str | None:
    """Reject a replacement before it overwrites a good file. Magic bytes, not the
    extension: a mis-picked .docx renamed to .pdf would otherwise land on disk."""
    if not content:
        return "empty"
    if len(content) > _MAX_ARTICLE_PDF_SIZE:
        return "too_large"
    if not content.startswith(b"%PDF"):
        return "not_pdf"
    return None


@app.post("/articles/{article_id}/replace-pdf")
async def replace_article_pdf(
    request: Request,
    article_id: int,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    session: Session = Depends(db.get_db),
):
    """Swap in a new PDF (or attach a first one) and re-parse the file's metadata."""
    article = session.get(Article, article_id)
    if article is None or article.hidden:
        raise HTTPException(status_code=404, detail="Article not found")

    pdf_bytes = await file.read()
    error = _pdf_upload_error(pdf_bytes)
    if error:
        if _wants_html(request):
            return RedirectResponse(
                f"/articles/{article_id}?pdf_error={error}", status_code=303
            )
        raise HTTPException(status_code=422, detail=_PDF_UPLOAD_ERRORS[error])

    result = ingest.replace_pdf(session, article, pdf_bytes)
    if result.doi_adopted:
        background_tasks.add_task(ingest.process_article, article_id)

    if _wants_html(request):
        params: dict[str, int] = {
            "pdf_replaced": 1,
            "pdf_keywords": len(result.keywords),
        }
        if result.abstract_updated:
            params["pdf_abstract"] = 1
        if result.affiliations_filled:
            params["pdf_affiliations"] = result.affiliations_filled
        if result.doi_adopted:
            params["pdf_doi"] = 1
        if result.conflicting_article_id:
            params["pdf_doi_conflict"] = result.conflicting_article_id
        return RedirectResponse(
            f"/articles/{article_id}?{urlencode(params)}", status_code=303
        )
    return {
        "article_id": article_id,
        "keywords": result.keywords,
        "abstract_updated": result.abstract_updated,
        "affiliations_filled": result.affiliations_filled,
        "doi_adopted": result.doi_adopted,
        "doi_conflict_article_id": result.conflicting_article_id,
    }


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
        "publication_type": article.publication_type,
        "doi": article.doi,
        "title": article.title,
        "year": article.year,
        "journal": article.journal,
        "conference_name": article.conference_name,
        "proceedings_title": article.proceedings_title,
        "conference_location": article.conference_location,
        "conference_start_date": article.conference_start_date,
        "conference_end_date": article.conference_end_date,
        "venue_name": article.venue_name,
        "status": article.status,
        "has_pdf": article.pdf_path is not None,
        "authors": [a.full_name for a in article.authors],
        "keywords": [k.name for k in article.keywords],
        "topics": [t.name for t in article.topics],
    }


def _project_or_404(session: Session, project_id: str) -> Project:
    project = session.scalar(
        select(Project)
        .where(Project.id == project_id)
        .options(selectinload(Project.documents))
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _project_page_context(
    request: Request,
    project: Project,
    *,
    error: str | None = None,
    project_number_value: str | None = None,
) -> dict:
    documents = [document for document in project.documents if document.status != "archived"]
    return {
        "project": project,
        "deliverables": [
            document
            for document in documents
            if document.document_type == "deliverable"
        ],
        "milestones": [
            document
            for document in documents
            if document.document_type == "milestone"
        ],
        "error": error,
        "project_number_value": (
            project.project_number
            if project_number_value is None
            else project_number_value
        ),
        "project_number_updated": request.query_params.get(
            "project_number_updated"
        )
        == "1",
    }


@app.get("/projects")
def project_list(request: Request, session: Session = Depends(db.get_db)):
    projects = session.scalars(
        select(Project)
        .options(selectinload(Project.documents))
        .order_by(Project.created_at.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "projects.html",
        {"projects": projects},
    )


@app.get("/projects/new")
def new_project_page(request: Request):
    return templates.TemplateResponse(request, "project_new.html", {})


@app.post("/projects")
def create_project(
    request: Request,
    name: str = Form(...),
    acronym: str | None = Form(None),
    project_number: str = Form(...),
    description: str | None = Form(None),
    session: Session = Depends(db.get_db),
):
    name = name.strip()
    acronym = _clean(acronym)
    project_number = project_number.strip()
    description = _clean(description)
    if not name or not project_number:
        return templates.TemplateResponse(
            request,
            "project_new.html",
            {
                "error": "Project name and project number are required.",
                "name": name,
                "acronym": acronym or "",
                "project_number": project_number,
                "description": description or "",
            },
            status_code=422,
        )

    project = Project(
        name=name,
        acronym=acronym,
        project_number=project_number,
        description=description,
    )
    session.add(project)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return templates.TemplateResponse(
            request,
            "project_new.html",
            {
                "error": "A project with this acronym or project number already exists.",
                "name": name,
                "acronym": acronym or "",
                "project_number": project_number,
                "description": description or "",
            },
            status_code=409,
        )
    return RedirectResponse(f"/projects/{project.id}", status_code=303)


@app.get("/projects/{project_id}")
def project_detail(
    request: Request,
    project_id: str,
    session: Session = Depends(db.get_db),
):
    project = _project_or_404(session, project_id)
    return templates.TemplateResponse(
        request,
        "project_detail.html",
        _project_page_context(request, project),
    )


@app.post("/projects/{project_id}/project-number")
def update_project_number(
    request: Request,
    project_id: str,
    project_number: str = Form(...),
    session: Session = Depends(db.get_db),
):
    project = _project_or_404(session, project_id)
    project_number = project_number.strip()
    if not project_number:
        return templates.TemplateResponse(
            request,
            "project_detail.html",
            _project_page_context(
                request,
                project,
                error="Project number is required.",
                project_number_value=project_number,
            ),
            status_code=422,
        )

    project.project_number = project_number
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        project = _project_or_404(session, project_id)
        return templates.TemplateResponse(
            request,
            "project_detail.html",
            _project_page_context(
                request,
                project,
                error=f"Another project already uses number {project_number}.",
                project_number_value=project_number,
            ),
            status_code=409,
        )
    return RedirectResponse(
        f"/projects/{project.id}?project_number_updated=1",
        status_code=303,
    )


def _next_project_entity_key(
    session: Session,
    project_id: str,
    document_type: str,
) -> str:
    prefix = "D" if document_type == "deliverable" else "MS"
    existing = set(
        session.scalars(
            select(ProjectDocument.external_entity_key).where(
                ProjectDocument.local_project_id == project_id,
                ProjectDocument.document_type == document_type,
            )
        )
    )
    number = 1
    while f"{prefix}{number}" in existing:
        number += 1
    return f"{prefix}{number}"


@app.post("/projects/{project_id}/documents")
async def upload_project_document(
    request: Request,
    project_id: str,
    file: UploadFile = File(...),
    document_type: str = Form(...),
    entity_key: str | None = Form(None),
    title: str | None = Form(None),
    description: str | None = Form(None),
    session: Session = Depends(db.get_db),
):
    project = _project_or_404(session, project_id)
    if document_type not in {"deliverable", "milestone"}:
        return templates.TemplateResponse(
            request,
            "project_detail.html",
            _project_page_context(
                request, project, error="Choose deliverable or milestone."
            ),
            status_code=422,
        )

    original_filename = Path(file.filename or "").name
    suffix = Path(original_filename).suffix.lower()
    content = await file.read()
    if not original_filename or suffix not in _PROJECT_FILE_SUFFIXES:
        error = "Choose a PDF, Word, Excel, PowerPoint, or text document."
    elif not content:
        error = "The selected file is empty."
    elif len(content) > _MAX_PROJECT_FILE_SIZE:
        error = "The selected file is larger than 50 MB."
    else:
        error = None
    if error:
        return templates.TemplateResponse(
            request,
            "project_detail.html",
            _project_page_context(request, project, error=error),
            status_code=422,
        )

    parsed = (
        project_document_parser.parse_project_document_pdf(content)
        if suffix == ".pdf"
        else project_document_parser.ParsedProjectDocument()
    )
    target_project = project
    if parsed.project_number:
        matched_project = session.scalar(
            select(Project).where(Project.project_number == parsed.project_number)
        )
        if matched_project is None:
            return templates.TemplateResponse(
                request,
                "project_detail.html",
                _project_page_context(
                    request,
                    project,
                    error=(
                        f"The PDF belongs to project {parsed.project_number}, "
                        "but no project with that number exists."
                    ),
                ),
                status_code=422,
            )
        target_project = matched_project

    document_type = parsed.document_type or document_type
    entity_key = _clean(entity_key) or parsed.entity_key or _next_project_entity_key(
        session, target_project.id, document_type
    )
    document_class = (
        DeliverableDocument
        if document_type == "deliverable"
        else MilestoneDocument
    )
    stored_path = db.PROJECT_DOCUMENT_DIR / f"{uuid4()}{suffix}"
    stored_path.write_bytes(content)
    document = document_class(
        source_system="refbase",
        local_project_id=target_project.id,
        external_project_id=target_project.id,
        external_entity_key=entity_key,
        title=_clean(title) or parsed.title or Path(original_filename).stem,
        description=_clean(description),
        lead_beneficiary=parsed.lead_beneficiary,
        authors_json=project_document_parser.encode_authors(parsed.authors),
        published_date=parsed.published_date,
        pdf_text=parsed.full_text,
        parse_status=(
            "parsed"
            if suffix == ".pdf" and not parsed.warnings
            else "partial"
            if suffix == ".pdf"
            else "not_parsed"
        ),
        parse_warnings=project_document_parser.encode_warnings(parsed.warnings),
        original_filename=original_filename,
        file_path=str(stored_path),
        mime_type=file.content_type or mimetypes.guess_type(original_filename)[0],
        byte_size=len(content),
    )
    session.add(document)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        stored_path.unlink(missing_ok=True)
        project = _project_or_404(session, project_id)
        return templates.TemplateResponse(
            request,
            "project_detail.html",
            _project_page_context(
                request,
                project,
                error=f"A {document_type} with code {entity_key} already exists.",
            ),
            status_code=409,
        )
    return RedirectResponse(f"/projects/{target_project.id}", status_code=303)


@app.get("/project-documents/{document_id}/file")
def project_document_file(
    document_id: str,
    session: Session = Depends(db.get_db),
    download: int = 0,
):
    document = session.get(ProjectDocument, document_id)
    if document is None or document.status == "archived" or not document.file_path:
        raise HTTPException(status_code=404, detail="Project document file not found")
    path = Path(document.file_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Project document file not found")
    return FileResponse(
        path,
        media_type=document.mime_type or "application/octet-stream",
        filename=document.original_filename or path.name,
        content_disposition_type="attachment" if download else "inline",
    )


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

# How many newest-first papers the dashboard's "Recent additions" panel lists.
_RECENT_ADDITIONS = 5


# --------------------------------------------------------------------------- keywords & topics

def _keyword_article_ids(keyword_id: int):
    """Subquery: the articles carrying one keyword."""
    return select(ArticleKeyword.article_id).where(
        ArticleKeyword.keyword_id == keyword_id
    )


def _topic_article_ids(topic_id: int):
    """Subquery: the articles carrying any keyword classified into one topic."""
    return (
        select(ArticleKeyword.article_id)
        .join(TopicKeyword, TopicKeyword.keyword_id == ArticleKeyword.keyword_id)
        .where(TopicKeyword.topic_id == topic_id)
    )


def _library_keyword_stats(session: Session) -> list:
    """(keyword, papers) for every keyword in use, most-used first."""
    paper_count = func.count(ArticleKeyword.article_id)
    return list(
        session.execute(
            select(Keyword, paper_count)
            .join(ArticleKeyword, ArticleKeyword.keyword_id == Keyword.id)
            .join(Article, Article.id == ArticleKeyword.article_id)
            .where(Article.hidden.is_(False))
            .group_by(Keyword.id)
            .order_by(paper_count.desc(), Keyword.name)
        ).all()
    )


def _topic_stats(session: Session) -> list:
    """(topic, papers, keywords) for every topic, newly created empty ones included.

    A paper counts once per topic however many of that topic's keywords it
    carries — collapsing that overlap is the point of the grouping.
    """
    paper_count = func.count(func.distinct(Article.id))
    keyword_count = func.count(func.distinct(TopicKeyword.keyword_id))
    return list(
        session.execute(
            select(Topic, paper_count, keyword_count)
            .outerjoin(TopicKeyword, TopicKeyword.topic_id == Topic.id)
            .outerjoin(
                ArticleKeyword, ArticleKeyword.keyword_id == TopicKeyword.keyword_id
            )
            .outerjoin(
                Article,
                and_(
                    Article.id == ArticleKeyword.article_id,
                    Article.hidden.is_(False),
                ),
            )
            .group_by(Topic.id)
            .order_by(paper_count.desc(), Topic.name)
        ).all()
    )


@app.get("/")
def index(
    request: Request,
    session: Session = Depends(db.get_db),
    sort: str = "year",
    order: str = "",
    keyword: int | None = None,
    topic: int | None = None,
    hidden_article: int = 0,
):
    if sort not in _SORT_KEYS:
        sort = "year"
    # Sensible default direction per column: newest-first for date/year, A→Z otherwise.
    if order not in ("asc", "desc"):
        order = "desc" if sort in ("recent", "year", "online") else "asc"
    descending = order == "desc"

    active_keyword = session.get(Keyword, keyword) if keyword is not None else None
    active_topic = session.get(Topic, topic) if topic is not None else None

    query = (
        select(Article)
        .where(Article.hidden.is_(False))
        .options(
            selectinload(Article.author_links).selectinload(ArticleAuthor.author),
            selectinload(Article.keywords).selectinload(Keyword.topics),
        )
    )
    if active_keyword is not None:  # one exact keyword
        query = query.where(Article.id.in_(_keyword_article_ids(active_keyword.id)))
    if active_topic is not None:  # any keyword classified into this topic
        query = query.where(Article.id.in_(_topic_article_ids(active_topic.id)))
    column = _SORT_COLUMNS.get(sort, Article.year)
    # Papers share a year (or a journal) constantly, so break ties on date added
    # instead of letting SQLite return them in whatever order it likes.
    query = query.order_by(
        column.desc() if descending else column.asc(), Article.created_at.desc()
    )
    # Unlimited: the "All research" heading carries a count, so the table has to
    # actually contain everything it claims. Revisit if the library outgrows it.
    articles = list(session.scalars(query))

    article_total = session.scalar(
        select(func.count(Article.id)).where(Article.hidden.is_(False))
    )

    if sort == "author":
        def first_author_surname(article: Article) -> tuple[int, str]:
            if not article.author_links:
                return (1, "")  # articles without authors sort last
            tokens = article.author_links[0].author.full_name.split()
            return (0, tokens[-1].lower() if tokens else "")

        articles.sort(key=first_author_surname, reverse=descending)

    # The "requested citing" table is the same rows filtered, not a second query,
    # so both tables always agree on ordering and on what the filters selected.
    cited_articles = [article for article in articles if article.cite_first]

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
            .where(ArticleAuthor.affiliation.ilike(f"%{NCNR_LABEL}%"))
        )
    }
    ncnr_authors = [pair for pair in author_stats if pair[0].id in ncnr_ids]
    other_authors = [pair for pair in author_stats if pair[0].id not in ncnr_ids]

    # Manually saved meeting shortcuts for authors currently represented in the
    # library. Phone-only contact records deliberately do not appear here.
    meeting_links = []
    saved_contacts = contacts.load_all()
    for author, _publication_count in author_stats:
        meeting_link = saved_contacts.get(str(author.id), {}).get("meeting_link")
        if meeting_link:
            meeting_links.append((author, meeting_link))
    meeting_links.sort(key=lambda pair: pair[0].full_name.casefold())

    # Newest arrivals, deliberately unaffected by the table's filter and sort: the
    # panel answers "what turned up lately", not "what is on screen".
    recent_articles = list(
        session.scalars(
            select(Article)
            .where(Article.hidden.is_(False))
            .options(
                selectinload(Article.author_links).selectinload(ArticleAuthor.author)
            )
            # Bulk ingests share a timestamp, so break the tie on insertion order.
            .order_by(Article.created_at.desc(), Article.id.desc())
            .limit(_RECENT_ADDITIONS)
        )
    )

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "articles": articles,
            "cited_articles": cited_articles,
            "article_total": article_total,
            "sort": sort,
            "order": order,
            "author_total": len(author_stats),
            "ncnr_authors": ncnr_authors,
            "other_authors": other_authors,
            "meeting_links": meeting_links,
            "recent_articles": recent_articles,
            "hidden_total": session.scalar(
                select(func.count(Article.id)).where(Article.hidden.is_(True))
            ),
            "just_hidden": bool(hidden_article),
            "ncnr_label": NCNR_LABEL,
            # Keywords across the library + how many articles carry each, and the
            # curated topics they roll up into.
            "keyword_stats": _library_keyword_stats(session),
            "topic_stats": _topic_stats(session),
            "active_keyword": active_keyword,
            "active_topic": active_topic,
        },
    )


@app.post("/articles/{article_id}/cite-first")
def toggle_cite_first(
    article_id: int,
    session: Session = Depends(db.get_db),
    sort: str = Form("year"),
    order: str = Form(""),
    keyword: int | None = Form(None),
    topic: int | None = Form(None),
    return_to: str | None = Form(None),
):
    """Flip the "cite this one first" flag; anyone can toggle it from the table."""
    article = session.get(Article, article_id)
    if article is None or article.hidden:
        raise HTTPException(status_code=404, detail="Article not found")
    article.cite_first = not article.cite_first
    session.commit()
    if return_to and return_to.startswith("/") and not return_to.startswith("//"):
        return RedirectResponse(return_to, status_code=303)
    params = {}
    if sort and sort != "year":  # "year" is the dashboard default; no need to spell it out
        params["sort"] = sort
    if order:
        params["order"] = order
    if keyword is not None:
        params["keyword"] = keyword
    if topic is not None:
        params["topic"] = topic
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return RedirectResponse(f"/{'?' + query if query else ''}", status_code=303)


@app.get("/upload")
def upload_page(request: Request):
    return templates.TemplateResponse(request, "upload.html", {})


@app.get("/search")
def search_page(
    request: Request,
    session: Session = Depends(db.get_db),
    q: str = "",
    sort: str = "relevance",
    order: str = "",
):
    results = []
    if q.strip():
        for article_id, snippet in db.search_articles(session, q):
            article = session.get(Article, article_id)
            if article and not article.hidden:
                results.append(
                    {
                        "article": article,
                        "snippet": snippet,
                        "count": db.count_search_matches(
                            q,
                            article.title,
                            article.abstract,
                            article.journal,
                            article.pdf_text,
                        ),
                    }
                )
    search_sort_keys = set(_PUB_SORT_KEYS) | {"count", "status"}
    if sort not in search_sort_keys and sort != "relevance":
        sort = "relevance"
    if sort != "relevance":
        if order not in ("asc", "desc"):
            order = "desc" if sort in ("count", "recent", "year", "online") else "asc"

        def result_sort_key(hit: dict) -> tuple:
            if sort == "count":
                return hit["count"], hit["article"].created_at
            if sort == "status":
                article = hit["article"]
                return article.status.casefold(), article.created_at
            return _PUB_SORT_KEYS[sort](hit["article"])

        results.sort(key=result_sort_key, reverse=(order == "desc"))
    return templates.TemplateResponse(
        request,
        "search.html",
        {"q": q, "results": results, "sort": sort, "order": order},
    )


# Same default as the dashboard: a publication list reads by year, not by upload date.
# Every key breaks ties on date added so equal years keep a stable order.
def _publication_first_author(article: Article) -> tuple[str, object]:
    name = article.authors[0].full_name if article.authors else ""
    surname = name.split()[-1].lower() if name.split() else ""
    return surname, article.created_at


_PUB_SORT_KEYS = {
    "recent": lambda a: (a.created_at,),
    "title": lambda a: ((a.title or "").lower(), a.created_at),
    "author": _publication_first_author,
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
    keyword: int | None = None,
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
                selectinload(Article.keywords),
            )
            .order_by(Article.created_at.desc())
        )
    )
    co_authors: dict[int, Author] = {}
    joint_counts: dict[int, int] = {}
    keywords: dict[int, Keyword] = {}
    keyword_counts: dict[int, int] = {}
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
                        key, display = canonical_affiliation(aff)
                        affiliations.setdefault(key, display)
                        if key == NCNR_PHRASE:
                            is_ncbj = True
        for article_keyword in article.keywords:
            keywords[article_keyword.id] = article_keyword
            keyword_counts[article_keyword.id] = (
                keyword_counts.get(article_keyword.id, 0) + 1
            )
    # Most frequent collaborators first, then alphabetical.
    co_author_stats = sorted(
        ((co_authors[cid], joint_counts[cid]) for cid in co_authors),
        key=lambda pair: (-pair[1], pair[0].full_name),
    )

    # Optional keyword filter — narrows only the publications table below, leaving
    # the co-author / affiliation / keyword summaries computed over all papers.
    active_keyword = keywords.get(keyword) if keyword is not None else None
    if active_keyword is not None:
        articles = [
            article
            for article in articles
            if any(item.id == active_keyword.id for item in article.keywords)
        ]

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
            # (keyword, count) pairs — most-used keyword first, then alphabetical.
            "keyword_stats": sorted(
                ((keywords[kid], keyword_counts[kid]) for kid in keywords),
                key=lambda pair: (-pair[1], pair[0].name),
            ),
            "active_keyword": active_keyword,
            "is_ncbj": is_ncbj,
            "contact": contacts.get(author.id) if is_ncbj else {},
            "contact_saved": bool(saved),
        },
    )


@app.get("/journals/{journal_name:path}")
def journal_detail(
    request: Request,
    journal_name: str,
    session: Session = Depends(db.get_db),
    sort: str = "year",
    order: str = "",
    keyword: int | None = None,
):
    """List the library's publications from one journal."""
    articles = list(
        session.scalars(
            select(Article)
            .where(Article.journal == journal_name, Article.hidden.is_(False))
            .options(
                selectinload(Article.author_links).selectinload(ArticleAuthor.author),
                selectinload(Article.keywords),
            )
            .order_by(Article.created_at.desc())
        )
    )
    if not articles:
        raise HTTPException(status_code=404, detail="Journal not found")

    keywords: dict[int, Keyword] = {}
    keyword_counts: dict[int, int] = {}
    for article in articles:
        for article_keyword in article.keywords:
            keywords[article_keyword.id] = article_keyword
            keyword_counts[article_keyword.id] = (
                keyword_counts.get(article_keyword.id, 0) + 1
            )

    # As on the author page, filtering changes the table but leaves the keyword
    # summary based on all of this journal's publications.
    active_keyword = keywords.get(keyword) if keyword is not None else None
    if active_keyword is not None:
        articles = [
            article
            for article in articles
            if any(item.id == active_keyword.id for item in article.keywords)
        ]

    if sort not in _PUB_SORT_KEYS:
        sort = "year"
    if order not in ("asc", "desc"):
        order = "desc" if sort in ("recent", "year", "online") else "asc"
    articles = sorted(
        articles, key=_PUB_SORT_KEYS[sort], reverse=(order == "desc")
    )

    return templates.TemplateResponse(
        request,
        "journal.html",
        {
            "journal": journal_name,
            "articles": articles,
            "sort": sort,
            "order": order,
            "keyword_stats": sorted(
                (
                    (keywords[keyword_id], keyword_counts[keyword_id])
                    for keyword_id in keywords
                ),
                key=lambda pair: (-pair[1], pair[0].name),
            ),
            "active_keyword": active_keyword,
        },
    )


def _topic_or_404(session: Session, topic_id: int) -> Topic:
    topic = session.scalar(
        select(Topic).where(Topic.id == topic_id).options(selectinload(Topic.keywords))
    )
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    return topic


def _topics_page_context(
    session: Session,
    *,
    error: str | None = None,
    name: str = "",
    description: str = "",
) -> dict:
    keyword_stats = _library_keyword_stats(session)
    classified = set(session.scalars(select(TopicKeyword.keyword_id)))
    return {
        "topic_stats": _topic_stats(session),
        # What is still waiting to be classified — the working list for whoever
        # is building the topics up.
        "unclassified": [
            pair for pair in keyword_stats if pair[0].id not in classified
        ],
        "keyword_total": len(keyword_stats),
        "error": error,
        "name": name,
        "description": description,
    }


@app.get("/topics")
def topics_page(request: Request, session: Session = Depends(db.get_db)):
    """The topic overview: every group, its size, and the leftover keywords."""
    return templates.TemplateResponse(
        request, "topics.html", _topics_page_context(session)
    )


@app.post("/topics")
def create_topic(
    request: Request,
    name: str = Form(...),
    description: str | None = Form(None),
    session: Session = Depends(db.get_db),
):
    name = name.strip()
    description = _clean(description)
    if not name:
        return templates.TemplateResponse(
            request,
            "topics.html",
            _topics_page_context(
                session, error="A topic needs a name.", description=description or ""
            ),
            status_code=422,
        )
    topic = Topic(name=name, description=description)
    session.add(topic)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return templates.TemplateResponse(
            request,
            "topics.html",
            _topics_page_context(
                session,
                error=f"A topic called “{name}” already exists.",
                name=name,
                description=description or "",
            ),
            status_code=409,
        )
    return RedirectResponse(f"/topics/{topic.id}", status_code=303)


def _topic_detail_context(
    session: Session,
    topic: Topic,
    *,
    error: str | None = None,
    saved: bool = False,
) -> dict:
    assigned = {keyword.id for keyword in topic.keywords}
    # Every keyword in the library is offered, so classifying one is a single
    # click here rather than an edit on each paper.
    picker = _library_keyword_stats(session)
    listed = {keyword.id for keyword, _count in picker}
    picker += [
        (keyword, 0) for keyword in topic.keywords if keyword.id not in listed
    ]
    articles = list(
        session.scalars(
            select(Article)
            .where(
                Article.hidden.is_(False),
                Article.id.in_(_topic_article_ids(topic.id)),
            )
            .options(
                selectinload(Article.author_links).selectinload(ArticleAuthor.author),
                selectinload(Article.keywords),
            )
            .order_by(Article.published_date.desc(), Article.created_at.desc())
        )
    )
    return {
        "topic": topic,
        "articles": articles,
        "keyword_picker": picker,
        "assigned_ids": assigned,
        "error": error,
        "saved": saved,
    }


@app.get("/topics/{topic_id}")
def topic_detail(
    request: Request,
    topic_id: int,
    session: Session = Depends(db.get_db),
    saved: int = 0,
):
    """One topic: its papers, and the keyword picker that defines it."""
    topic = _topic_or_404(session, topic_id)
    return templates.TemplateResponse(
        request,
        "topic_detail.html",
        _topic_detail_context(session, topic, saved=bool(saved)),
    )


@app.post("/topics/{topic_id}")
def update_topic(
    request: Request,
    topic_id: int,
    name: str = Form(...),
    description: str | None = Form(None),
    session: Session = Depends(db.get_db),
):
    topic = _topic_or_404(session, topic_id)
    name = name.strip()
    if not name:
        return templates.TemplateResponse(
            request,
            "topic_detail.html",
            _topic_detail_context(session, topic, error="A topic needs a name."),
            status_code=422,
        )
    topic.name = name
    topic.description = _clean(description)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        topic = _topic_or_404(session, topic_id)
        return templates.TemplateResponse(
            request,
            "topic_detail.html",
            _topic_detail_context(
                session, topic, error=f"Another topic is already called “{name}”."
            ),
            status_code=409,
        )
    return RedirectResponse(f"/topics/{topic_id}?saved=1", status_code=303)


@app.post("/topics/{topic_id}/keywords")
def toggle_topic_keyword(
    request: Request,
    topic_id: int,
    keyword_id: int = Form(...),
    session: Session = Depends(db.get_db),
):
    """Click a keyword in the topic's picker to classify it — or to take it out."""
    topic = _topic_or_404(session, topic_id)
    keyword = session.get(Keyword, keyword_id)
    if keyword is None:
        raise HTTPException(status_code=404, detail="Keyword not found")
    link = session.get(TopicKeyword, {"topic_id": topic.id, "keyword_id": keyword.id})
    if link is None:
        session.add(TopicKeyword(topic_id=topic.id, keyword_id=keyword.id))
        assigned = True
    else:
        session.delete(link)
        assigned = False
    session.commit()
    if _wants_html(request):
        return RedirectResponse(f"/topics/{topic_id}", status_code=303)
    return {"topic_id": topic.id, "keyword_id": keyword.id, "assigned": assigned}


@app.post("/topics/{topic_id}/delete")
def delete_topic(
    request: Request, topic_id: int, session: Session = Depends(db.get_db)
):
    """Drop a grouping. Keywords and papers are untouched — only the grouping goes."""
    topic = _topic_or_404(session, topic_id)
    # Deleting the topic drops its topic_keywords rows; the keywords themselves
    # belong to the papers, not to the grouping.
    session.delete(topic)
    session.commit()
    if _wants_html(request):
        return RedirectResponse("/topics", status_code=303)
    return {"deleted": topic_id}


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
    pdf_replaced: int = 0,
    pdf_keywords: int = 0,
    pdf_abstract: int = 0,
    pdf_affiliations: int = 0,
    pdf_doi: int = 0,
    pdf_doi_conflict: int = 0,
    pdf_error: str | None = None,
    restored: int = 0,
    citations_saved: int = 0,
    highlights_fetched: int = 0,
    highlights_failed: int = 0,
    highlights_saved: int = 0,
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
            "pdf_replaced": bool(pdf_replaced),
            "pdf_keywords": pdf_keywords,
            "pdf_abstract": bool(pdf_abstract),
            "pdf_affiliations": pdf_affiliations,
            "pdf_doi": bool(pdf_doi),
            "pdf_doi_conflict": pdf_doi_conflict,
            "pdf_error": _PDF_UPLOAD_ERRORS.get(pdf_error or ""),
            "restored": bool(restored),
            "citations_saved": bool(citations_saved),
            "highlights_fetched": bool(highlights_fetched),
            "highlights_failed": bool(highlights_failed),
            "highlights_saved": bool(highlights_saved),
        },
    )


@app.post("/articles/{article_id}/fetch-highlights")
def fetch_article_highlights(
    article_id: int,
    session: Session = Depends(db.get_db),
):
    article = session.get(Article, article_id)
    if article is None or article.hidden:
        raise HTTPException(status_code=404, detail="Article not found")
    if not article.doi:
        raise HTTPException(status_code=400, detail="Article has no DOI")
    if article.highlights:
        return RedirectResponse(
            f"/articles/{article_id}?highlights_fetched=1", status_code=303
        )

    highlights = ingest.fetch_publisher_highlights(article.doi)
    if not highlights:
        return RedirectResponse(
            f"/articles/{article_id}?highlights_failed=1", status_code=303
        )
    article.highlights = "\n".join(highlights)
    session.commit()
    return RedirectResponse(
        f"/articles/{article_id}?highlights_fetched=1", status_code=303
    )


@app.post("/articles/{article_id}/citation-examples")
def save_article_citation_examples(
    article_id: int,
    session: Session = Depends(db.get_db),
    citation_examples: str | None = Form(None),
):
    article = session.get(Article, article_id)
    if article is None or article.hidden:
        raise HTTPException(status_code=404, detail="Article not found")
    article.citation_examples = _clean(citation_examples)
    session.commit()
    return RedirectResponse(
        f"/articles/{article_id}?citations_saved=1", status_code=303
    )


@app.post("/articles/{article_id}/highlights")
def save_article_highlights(
    article_id: int,
    session: Session = Depends(db.get_db),
    highlights: str | None = Form(None),
):
    article = session.get(Article, article_id)
    if article is None or article.hidden:
        raise HTTPException(status_code=404, detail="Article not found")
    article.highlights = _clean(highlights)
    session.commit()
    return RedirectResponse(
        f"/articles/{article_id}?highlights_saved=1", status_code=303
    )


@app.post("/articles/{article_id}/hide")
def hide_article(
    request: Request, article_id: int, session: Session = Depends(db.get_db)
):
    """Soft delete: the record drops out of every listing but is never destroyed.

    The way to clear an accidental duplicate. `/hidden` lists what has been put
    away and offers it back.
    """
    article = session.get(Article, article_id)
    if article is None or article.hidden:
        raise HTTPException(status_code=404, detail="Article not found")
    article.hidden = True
    session.commit()
    if _wants_html(request):
        return RedirectResponse("/?hidden_article=1", status_code=303)
    return {"article_id": article_id, "hidden": True}


@app.post("/articles/{article_id}/unhide")
def unhide_article(
    request: Request, article_id: int, session: Session = Depends(db.get_db)
):
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    article.hidden = False
    session.commit()
    if _wants_html(request):
        return RedirectResponse(f"/articles/{article_id}?restored=1", status_code=303)
    return {"article_id": article_id, "hidden": False}


@app.get("/hidden")
def hidden_articles(request: Request, session: Session = Depends(db.get_db)):
    """The soft-deleted records, newest first, each restorable in one click."""
    articles = list(
        session.scalars(
            select(Article)
            .where(Article.hidden.is_(True))
            .options(
                selectinload(Article.author_links).selectinload(ArticleAuthor.author)
            )
            .order_by(Article.created_at.desc(), Article.id.desc())
        )
    )
    return templates.TemplateResponse(
        request, "hidden.html", {"articles": articles}
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


def _empty_selection(request: Request) -> RedirectResponse:
    """Nothing ticked. A browser gets sent back to the page it came from; an API
    client gets the 400 it can act on."""
    if _wants_html(request):
        return RedirectResponse(request.headers.get("referer") or "/", status_code=303)
    raise HTTPException(status_code=400, detail="No articles selected")


def _articles_for_export(session: Session, ids: list[int]) -> list[Article]:
    """Non-hidden articles for the selected ids, kept in the order they were ticked."""
    if not ids:
        raise HTTPException(status_code=400, detail="No articles selected")
    found = {
        article.id: article
        for article in session.scalars(
            select(Article)
            .where(Article.id.in_(ids), Article.hidden.is_(False))
            .options(
                selectinload(Article.author_links).selectinload(ArticleAuthor.author),
                selectinload(Article.keywords).selectinload(Keyword.topics),
            )
        )
    }
    articles = [found[i] for i in dict.fromkeys(ids) if i in found]
    if not articles:
        raise HTTPException(status_code=404, detail="None of the selected articles exist")
    return articles


def _share_token(session: Session) -> str:
    """Create a URL-safe bearer token that does not expose the selected IDs."""
    while True:
        token = secrets.token_urlsafe(24)
        if session.get(SharedSelection, token) is None:
            return token


@app.post("/shares", status_code=201, name="create_shared_selection")
def create_shared_selection(
    request: Request,
    ids: list[int] = Form(default=[]),
    session: Session = Depends(db.get_db),
):
    """Persist an ordered paper selection and return its shareable RefBase URL."""
    if not ids:
        return _empty_selection(request)
    articles = _articles_for_export(session, ids)
    selection = SharedSelection(
        token=_share_token(session),
        article_links=[
            SharedSelectionArticle(article_id=article.id, position=position)
            for position, article in enumerate(articles)
        ],
    )
    session.add(selection)
    session.commit()

    share_url = request.url_for("shared_selection_page", token=selection.token)
    if _wants_html(request):
        return RedirectResponse(f"{share_url}?created=1", status_code=303)
    return {
        "token": selection.token,
        "url": str(share_url),
        "article_count": len(articles),
    }


@app.get("/shares/{token}", name="shared_selection_page")
def shared_selection_page(
    request: Request,
    token: str,
    created: int = 0,
    session: Session = Depends(db.get_db),
):
    """Show the current RefBase metadata for one persistent shared selection."""
    selection = session.get(SharedSelection, token)
    if selection is None:
        raise HTTPException(status_code=404, detail="Shared selection not found")

    articles = list(
        session.scalars(
            select(Article)
            .join(
                SharedSelectionArticle,
                SharedSelectionArticle.article_id == Article.id,
            )
            .where(
                SharedSelectionArticle.selection_token == token,
                Article.hidden.is_(False),
            )
            .order_by(SharedSelectionArticle.position)
            .options(
                selectinload(Article.author_links).selectinload(ArticleAuthor.author),
                selectinload(Article.keywords).selectinload(Keyword.topics),
            )
        )
    )
    share_url = str(request.url_for("shared_selection_page", token=token))
    return Response(
        site_export.render_shared_page(
            articles,
            share_url=share_url,
            created_at=selection.created_at,
            link_created=created == 1,
        ),
        media_type="text/html",
    )


@app.post("/export/bibtex", response_class=PlainTextResponse)
def export_bibtex(
    request: Request,
    ids: list[int] = Form(default=[]),
    session: Session = Depends(db.get_db),
):
    """Merge the selected articles into one .bib (cite keys deduplicated across it)."""
    if not ids:
        return _empty_selection(request)
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
    request: Request,
    ids: list[int] = Form(default=[]),
    session: Session = Depends(db.get_db),
):
    """Same selection as the .bib export, as one Word Source Manager document."""
    if not ids:
        return _empty_selection(request)
    articles = _articles_for_export(session, ids)
    return PlainTextResponse(
        word_xml.export_word_xml(articles),
        media_type="application/xml",
        headers={
            "Content-Disposition": f'attachment; filename="refbase-{len(articles)}-refs.xml"'
        },
    )


def _unique_archive_name(name: str, used: set[str]) -> str:
    """Keep ZIP members distinct when selected articles produce the same cite key."""
    candidate = name
    stem, suffix = Path(name).stem, Path(name).suffix
    number = 2
    while candidate in used:
        candidate = f"{stem}-{number}{suffix}"
        number += 1
    used.add(candidate)
    return candidate


def _pdf_archive(articles: list[Article], include_references: bool) -> bytes:
    output = io.BytesIO()
    used_names: set[str] = set()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if include_references:
            archive.writestr(
                "refbase-selected.bib", bibtex.export_bibtex(articles)
            )
            archive.writestr(
                "refbase-selected.xml", word_xml.export_word_xml(articles)
            )
        for article in articles:
            if not article.pdf_path:
                continue
            pdf_path = Path(article.pdf_path)
            if not pdf_path.is_file():
                continue
            name = _unique_archive_name(bibtex.pdf_filename(article), used_names)
            archive.write(pdf_path, arcname=f"pdfs/{name}")
    return output.getvalue()


@app.post("/export/pdfs")
def export_pdfs(
    request: Request,
    ids: list[int] = Form(default=[]),
    session: Session = Depends(db.get_db),
):
    """Download all available PDFs among the selected publications."""
    if not ids:
        return _empty_selection(request)
    articles = _articles_for_export(session, ids)
    has_pdf = any(
        article.pdf_path and Path(article.pdf_path).is_file()
        for article in articles
    )
    if not has_pdf:
        raise HTTPException(status_code=404, detail="No selected articles have a PDF")
    return Response(
        _pdf_archive(articles, include_references=False),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="refbase-{len(articles)}-pdfs.zip"'
        },
    )


@app.post("/export/all")
def export_all_formats(
    request: Request,
    ids: list[int] = Form(default=[]),
    session: Session = Depends(db.get_db),
):
    """Download merged BibTeX, Word XML, and all available PDFs in one ZIP."""
    if not ids:
        return _empty_selection(request)
    articles = _articles_for_export(session, ids)
    return Response(
        _pdf_archive(articles, include_references=True),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="refbase-{len(articles)}-all.zip"'
        },
    )


@app.post("/export/site")
def export_site(
    request: Request,
    ids: list[int] = Form(default=[]),
    session: Session = Depends(db.get_db),
):
    """Download the selection as a browsable offline copy of this page + its PDFs."""
    if not ids:
        return _empty_selection(request)
    articles = _articles_for_export(session, ids)
    return Response(
        site_export.build_archive(articles),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="refbase-{len(articles)}-page.zip"'
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
