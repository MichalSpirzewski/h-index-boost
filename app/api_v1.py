"""Versioned metadata API for publications and project documents.

File content and document versions intentionally remain outside this first
contract. The API exposes logical objects that those resources can attach to.
"""

from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import db, ingest
from app.models import (
    ArticleAuthor,
    ConferencePublication,
    DeliverableDocument,
    JournalPublication,
    MilestoneDocument,
    ProjectDocument,
    Publication,
)

PublicationType = Literal["journal", "conference"]
ProjectDocumentType = Literal["deliverable", "milestone"]
DbSession = Annotated[Session, Depends(db.get_db)]


def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    """Require X-API-Key when REFBASE_API_KEY is configured.

    Local development stays open until a key is set. Production can enable the
    guard without changing the API contract or application code.
    """
    expected = os.environ.get("REFBASE_API_KEY", "")
    if expected and (not x_api_key or not secrets.compare_digest(x_api_key, expected)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key.",
        )


router = APIRouter(
    prefix="/api/v1",
    tags=["API v1"],
    dependencies=[Depends(require_api_key)],
)


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicationCreate(ApiModel):
    publication_type: PublicationType
    title: str = Field(min_length=1)
    doi: str | None = None
    year: int | None = Field(default=None, ge=1000, le=9999)
    published_date: str | None = None
    online_date: str | None = None
    journal: str | None = None
    conference_name: str | None = None
    proceedings_title: str | None = None
    conference_location: str | None = None
    conference_start_date: str | None = None
    conference_end_date: str | None = None
    abstract: str | None = None
    source_url: str | None = None
    added_by: str | None = None


class PublicationUpdate(ApiModel):
    title: str | None = Field(default=None, min_length=1)
    year: int | None = Field(default=None, ge=1000, le=9999)
    published_date: str | None = None
    online_date: str | None = None
    journal: str | None = None
    conference_name: str | None = None
    proceedings_title: str | None = None
    conference_location: str | None = None
    conference_start_date: str | None = None
    conference_end_date: str | None = None
    abstract: str | None = None
    source_url: str | None = None


class PublicationResponse(ApiModel):
    id: int
    publication_type: str
    doi: str | None
    title: str | None
    year: int | None
    published_date: str | None
    online_date: str | None
    journal: str | None
    conference_name: str | None
    proceedings_title: str | None
    conference_location: str | None
    conference_start_date: str | None
    conference_end_date: str | None
    abstract: str | None
    source_url: str | None
    added_by: str | None
    venue_name: str | None
    status: str
    hidden: bool
    has_pdf: bool
    authors: list[str]
    topics: list[str]
    created_at: datetime


class ProjectDocumentCreate(ApiModel):
    document_type: ProjectDocumentType
    external_project_id: str = Field(min_length=1)
    external_entity_key: str = Field(min_length=1)
    source_system: str = Field(default="doc-workflow", min_length=1)
    title: str | None = None
    description: str | None = None


class ProjectDocumentUpdate(ApiModel):
    title: str | None = None
    description: str | None = None


class ProjectDocumentResponse(ApiModel):
    id: str
    document_type: str
    source_system: str
    local_project_id: str | None
    project_number: str | None
    external_project_id: str
    external_entity_key: str | None
    title: str | None
    description: str | None
    lead_beneficiary: str | None
    authors: list[str]
    published_date: str | None
    parse_status: str
    parse_warnings: list[str]
    original_filename: str | None
    mime_type: str | None
    byte_size: int | None
    has_file: bool
    status: str
    archived_at: datetime | None
    created_at: datetime


def _strip(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _publication_response(publication: Publication) -> PublicationResponse:
    return PublicationResponse(
        id=publication.id,
        publication_type=publication.publication_type,
        doi=publication.doi,
        title=publication.title,
        year=publication.year,
        published_date=publication.published_date,
        online_date=publication.online_date,
        journal=publication.journal,
        conference_name=publication.conference_name,
        proceedings_title=publication.proceedings_title,
        conference_location=publication.conference_location,
        conference_start_date=publication.conference_start_date,
        conference_end_date=publication.conference_end_date,
        abstract=publication.abstract,
        source_url=publication.source_url,
        added_by=publication.added_by,
        venue_name=publication.venue_name,
        status=publication.status,
        hidden=publication.hidden,
        has_pdf=publication.pdf_path is not None,
        authors=[author.full_name for author in publication.authors],
        topics=[topic.name for topic in publication.topics],
        created_at=publication.created_at,
    )


def _project_document_response(document: ProjectDocument) -> ProjectDocumentResponse:
    return ProjectDocumentResponse(
        id=document.id,
        document_type=document.document_type,
        source_system=document.source_system,
        local_project_id=document.local_project_id,
        project_number=(
            document.local_project.project_number if document.local_project else None
        ),
        external_project_id=document.external_project_id,
        external_entity_key=document.external_entity_key,
        title=document.title,
        description=document.description,
        lead_beneficiary=document.lead_beneficiary,
        authors=document.project_authors,
        published_date=document.published_date,
        parse_status=document.parse_status,
        parse_warnings=document.parsing_warnings,
        original_filename=document.original_filename,
        mime_type=document.mime_type,
        byte_size=document.byte_size,
        has_file=document.file_path is not None,
        status=document.status,
        archived_at=document.archived_at,
        created_at=document.created_at,
    )


def _publication_or_404(session: Session, publication_id: int) -> Publication:
    publication = session.scalar(
        select(Publication)
        .where(Publication.id == publication_id)
        .options(
            selectinload(Publication.author_links).selectinload(ArticleAuthor.author),
            selectinload(Publication.topics),
        )
    )
    if publication is None:
        raise HTTPException(status_code=404, detail="Publication not found.")
    return publication


def _project_document_or_404(session: Session, document_id: str) -> ProjectDocument:
    document = session.get(ProjectDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Project document not found.")
    return document


@router.post(
    "/publications",
    response_model=PublicationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_publication(
    body: PublicationCreate,
    session: DbSession,
) -> PublicationResponse:
    doi = None
    if body.doi:
        doi = ingest.extract_doi(body.doi)
        if doi is None:
            raise HTTPException(status_code=422, detail="Invalid DOI.")

    values = body.model_dump(exclude={"publication_type", "doi"})
    values = {
        key: _strip(value) if isinstance(value, str) else value
        for key, value in values.items()
    }
    publication_class = (
        JournalPublication
        if body.publication_type == "journal"
        else ConferencePublication
    )
    publication = publication_class(doi=doi, status="ready", **values)
    session.add(publication)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="Publication already exists.") from exc
    session.refresh(publication)
    return _publication_response(publication)


@router.get("/publications", response_model=list[PublicationResponse])
def list_publications(
    session: DbSession,
    publication_type: Annotated[PublicationType | None, Query()] = None,
    include_hidden: Annotated[bool, Query()] = False,
) -> list[PublicationResponse]:
    query = select(Publication).options(
        selectinload(Publication.author_links).selectinload(ArticleAuthor.author),
        selectinload(Publication.topics),
    )
    if publication_type:
        query = query.where(Publication.publication_type == publication_type)
    if not include_hidden:
        query = query.where(Publication.hidden.is_(False))
    query = query.order_by(Publication.created_at.desc())
    return [_publication_response(item) for item in session.scalars(query).all()]


@router.get("/publications/{publication_id}", response_model=PublicationResponse)
def get_publication(
    publication_id: int,
    session: DbSession,
) -> PublicationResponse:
    return _publication_response(_publication_or_404(session, publication_id))


@router.patch("/publications/{publication_id}", response_model=PublicationResponse)
def update_publication(
    publication_id: int,
    body: PublicationUpdate,
    session: DbSession,
) -> PublicationResponse:
    publication = _publication_or_404(session, publication_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(publication, field, _strip(value) if isinstance(value, str) else value)
    session.commit()
    return _publication_response(publication)


@router.post(
    "/publications/{publication_id}/archive",
    response_model=PublicationResponse,
)
def archive_publication(
    publication_id: int,
    session: DbSession,
) -> PublicationResponse:
    publication = _publication_or_404(session, publication_id)
    publication.hidden = True
    session.commit()
    return _publication_response(publication)


@router.post(
    "/project-documents",
    response_model=ProjectDocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_project_document(
    body: ProjectDocumentCreate,
    session: DbSession,
) -> ProjectDocumentResponse:
    document_class = (
        DeliverableDocument
        if body.document_type == "deliverable"
        else MilestoneDocument
    )
    document = document_class(
        source_system=body.source_system.strip(),
        external_project_id=body.external_project_id.strip(),
        external_entity_key=body.external_entity_key.strip(),
        title=_strip(body.title),
        description=_strip(body.description),
    )
    session.add(document)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="A document for this project entity already exists.",
        ) from exc
    session.refresh(document)
    return _project_document_response(document)


@router.get(
    "/project-documents/{document_id}",
    response_model=ProjectDocumentResponse,
)
def get_project_document(
    document_id: str,
    session: DbSession,
) -> ProjectDocumentResponse:
    return _project_document_response(_project_document_or_404(session, document_id))


@router.get(
    "/projects/{external_project_id}/documents",
    response_model=list[ProjectDocumentResponse],
)
def list_project_documents(
    external_project_id: str,
    session: DbSession,
    document_type: Annotated[ProjectDocumentType | None, Query()] = None,
    include_archived: Annotated[bool, Query()] = False,
    source_system: Annotated[str, Query()] = "doc-workflow",
) -> list[ProjectDocumentResponse]:
    query = select(ProjectDocument).where(
        ProjectDocument.external_project_id == external_project_id,
        ProjectDocument.source_system == source_system,
    )
    if document_type:
        query = query.where(ProjectDocument.document_type == document_type)
    if not include_archived:
        query = query.where(ProjectDocument.status != "archived")
    query = query.order_by(ProjectDocument.created_at.desc())
    return [
        _project_document_response(document)
        for document in session.scalars(query).all()
    ]


@router.patch(
    "/project-documents/{document_id}",
    response_model=ProjectDocumentResponse,
)
def update_project_document(
    document_id: str,
    body: ProjectDocumentUpdate,
    session: DbSession,
) -> ProjectDocumentResponse:
    document = _project_document_or_404(session, document_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(document, field, _strip(value))
    session.commit()
    return _project_document_response(document)


@router.post(
    "/project-documents/{document_id}/archive",
    response_model=ProjectDocumentResponse,
)
def archive_project_document(
    document_id: str,
    session: DbSession,
) -> ProjectDocumentResponse:
    document = _project_document_or_404(session, document_id)
    document.status = "archived"
    document.archived_at = datetime.now(UTC)
    session.commit()
    return _project_document_response(document)
