from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import bibtex, db, ingest
from app.models import Article

app = FastAPI(title="RefBase")

_BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=_BASE_DIR / "templates")
app.mount("/static", StaticFiles(directory=_BASE_DIR / "static"), name="static")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


def _clean(value: str | None) -> str | None:
    """HTML forms send empty strings for blank fields; treat them as absent."""
    if value is None:
        return None
    value = value.strip()
    return value or None


# --------------------------------------------------------------------------- ingest API

@app.post("/api/ingest")
async def ingest_endpoint(
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
        raise HTTPException(status_code=422, detail="Provide a PDF, a URL, or a DOI.")

    # Resolve DOI: raw input -> URL -> PDF text/metadata.
    resolved = ingest.extract_doi(doi) or ingest.extract_doi(url)
    if not resolved and pdf_bytes:
        resolved = ingest.extract_doi_from_pdf(pdf_bytes)

    if resolved:
        existing = session.scalar(select(Article).where(Article.doi == resolved))
        if existing:
            return _duplicate_response(session, background_tasks, existing, pdf_bytes)

        article = Article(
            doi=resolved, title=title, source_url=url, added_by=added_by, status="pending"
        )
        if pdf_bytes:
            article.pdf_path = ingest.save_pdf(pdf_bytes, resolved)
        session.add(article)
        try:
            session.commit()
        except IntegrityError:  # lost a race on the UNIQUE(doi) constraint
            session.rollback()
            existing = session.scalar(select(Article).where(Article.doi == resolved))
            return _duplicate_response(session, background_tasks, existing, pdf_bytes)

        background_tasks.add_task(ingest.process_article, article.id)
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
    if pdf_bytes:
        article.pdf_path = ingest.save_pdf(pdf_bytes, None)
    session.add(article)
    session.commit()
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
        existing.pdf_path = ingest.save_pdf(pdf_bytes, existing.doi)
        session.commit()
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

@app.get("/")
def index(request: Request, session: Session = Depends(db.get_db)):
    articles = session.scalars(
        select(Article).where(Article.hidden.is_(False))
        .order_by(Article.created_at.desc()).limit(20)
    ).all()
    return templates.TemplateResponse(request, "index.html", {"articles": articles})


@app.get("/upload")
def upload_page(request: Request):
    return templates.TemplateResponse(request, "upload.html", {})


@app.get("/articles/{article_id}")
def article_detail(request: Request, article_id: int, session: Session = Depends(db.get_db)):
    article = session.get(Article, article_id)
    if article is None or article.hidden:
        raise HTTPException(status_code=404, detail="Article not found")
    return templates.TemplateResponse(request, "article.html", {"article": article})


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


@app.get("/articles/{article_id}/pdf")
def article_pdf(article_id: int, session: Session = Depends(db.get_db)):
    article = session.get(Article, article_id)
    if article is None or not article.pdf_path:
        raise HTTPException(status_code=404, detail="No PDF for this article")
    return FileResponse(article.pdf_path, media_type="application/pdf")
