from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Publication(Base):
    """A citable scientific output stored by the portal.

    The physical table keeps its original ``articles`` name for a backwards-
    compatible migration. ``publication_type`` enables SQLAlchemy single-table
    inheritance while shared metadata remains on this model.
    """

    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    publication_type: Mapped[str] = mapped_column(
        String, nullable=False, default="publication"
    )
    doi: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Issue date at whatever precision the publisher actually gave: "2025-11-11",
    # "2026-05" or "2022". A journal issue dated "May 2026" has no day, so the
    # precision is stored rather than padded to a date that does not exist.
    # Zero-padded, so lexicographic ordering is chronological ordering.
    published_date: Mapped[str | None] = mapped_column(String, nullable=True)
    # When the paper first appeared online (Crossref published-online, else created).
    # Usually a full date, and usually earlier than the issue it ends up in.
    online_date: Mapped[str | None] = mapped_column(String, nullable=True)
    journal: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Conference-specific venue metadata. These columns live in the shared table
    # because the hierarchy intentionally uses single-table inheritance.
    conference_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    proceedings_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    conference_location: Mapped[str | None] = mapped_column(Text, nullable=True)
    conference_start_date: Mapped[str | None] = mapped_column(String, nullable=True)
    conference_end_date: Mapped[str | None] = mapped_column(String, nullable=True)
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-form examples suggested by library users for citing this publication.
    citation_examples: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Publisher Highlights fetched manually from the DOI landing page, one per line.
    highlights: Mapped[str | None] = mapped_column(Text, nullable=True)
    crossref_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_by: Mapped[str | None] = mapped_column(String, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending/ready/metadata_failed
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    # Manually flagged by anyone as "cite this one first" (e.g. among near-duplicates
    # or a series of related papers) so co-authors know which version to reference.
    cite_first: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )

    author_links: Mapped[list[ArticleAuthor]] = relationship(
        back_populates="article",
        order_by="ArticleAuthor.position",
        cascade="all, delete-orphan",
    )
    topics: Mapped[list[Topic]] = relationship(
        secondary="article_topics", back_populates="articles"
    )

    __mapper_args__ = {
        "polymorphic_on": publication_type,
        "polymorphic_identity": "publication",
    }

    @property
    def authors(self) -> list[Author]:
        return [link.author for link in self.author_links]

    @property
    def venue_name(self) -> str | None:
        """A common display value for code that does not care about subtype."""
        return None


class JournalPublication(Publication):
    """A publication in a scientific journal."""

    __mapper_args__ = {"polymorphic_identity": "journal"}

    @property
    def venue_name(self) -> str | None:
        return self.journal


class ConferencePublication(Publication):
    """A paper published in conference proceedings."""

    __mapper_args__ = {"polymorphic_identity": "conference"}

    @property
    def venue_name(self) -> str | None:
        return self.conference_name or self.proceedings_title


# Compatibility for the existing routes, templates, exports, and third-party
# imports. New code should use Publication or one of its explicit subtypes.
Article = Publication


class Project(Base):
    """A local project workspace grouping non-publication documents."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    acronym: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    project_number: Mapped[str | None] = mapped_column(
        String, unique=True, nullable=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )

    documents: Mapped[list[ProjectDocument]] = relationship(
        back_populates="local_project",
        order_by="ProjectDocument.created_at.desc()",
        cascade="all, delete-orphan",
    )


class ProjectDocument(Base):
    """A document belonging to an externally managed scientific project.

    Project structure and access remain owned by the project-management service.
    These external references will let its API attach stored document content to
    a project, deliverable, or milestone without copying those domain objects.
    """

    __tablename__ = "project_documents"
    __table_args__ = (
        UniqueConstraint(
            "source_system",
            "external_project_id",
            "document_type",
            "external_entity_key",
            name="uq_project_document_external_entity",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    document_type: Mapped[str] = mapped_column(
        String, nullable=False, default="project_document"
    )
    source_system: Mapped[str] = mapped_column(
        String, nullable=False, default="doc-workflow"
    )
    local_project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    external_project_id: Mapped[str] = mapped_column(String, nullable=False)
    # A stable project-local code such as D2.1 or MS3. It is deliberately not a
    # database foreign key because the owning service may recreate its local rows.
    external_entity_key: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    lead_beneficiary: Mapped[str | None] = mapped_column(Text, nullable=True)
    authors_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_date: Mapped[str | None] = mapped_column(String, nullable=True)
    pdf_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    parse_status: Mapped[str] = mapped_column(String, nullable=False, default="not_parsed")
    parse_warnings: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String, nullable=True)
    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )

    __mapper_args__ = {
        "polymorphic_on": document_type,
        "polymorphic_identity": "project_document",
    }

    local_project: Mapped[Project | None] = relationship(back_populates="documents")

    @property
    def project_authors(self) -> list[str]:
        if not self.authors_json:
            return []
        try:
            value = json.loads(self.authors_json)
        except json.JSONDecodeError:
            return []
        return [str(item) for item in value] if isinstance(value, list) else []

    @property
    def parsing_warnings(self) -> list[str]:
        if not self.parse_warnings:
            return []
        try:
            value = json.loads(self.parse_warnings)
        except json.JSONDecodeError:
            return []
        return [str(item) for item in value] if isinstance(value, list) else []


class DeliverableDocument(ProjectDocument):
    """Stored content associated with one project deliverable."""

    __mapper_args__ = {"polymorphic_identity": "deliverable"}


class MilestoneDocument(ProjectDocument):
    """Stored content associated with one project milestone."""

    __mapper_args__ = {"polymorphic_identity": "milestone"}


class Author(Base):
    __tablename__ = "authors"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    orcid: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    # Duplicate grouping: a merged author points at its canonical ("base") author.
    # Canonical authors have NULL. Soft merge only — the row is never deleted.
    merged_into_id: Mapped[int | None] = mapped_column(
        ForeignKey("authors.id"), nullable=True
    )

    article_links: Mapped[list[ArticleAuthor]] = relationship(back_populates="author")

    @property
    def is_merged(self) -> bool:
        return self.merged_into_id is not None


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)

    articles: Mapped[list[Article]] = relationship(
        secondary="article_topics", back_populates="topics"
    )


class ArticleAuthor(Base):
    __tablename__ = "article_authors"

    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("authors.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    # This author's affiliation *on this paper* (Crossref if present, else PDF-parsed).
    affiliation: Mapped[str | None] = mapped_column(Text, nullable=True)

    article: Mapped[Article] = relationship(back_populates="author_links")
    author: Mapped[Author] = relationship(back_populates="article_links")


class ArticleTopic(Base):
    __tablename__ = "article_topics"

    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"), primary_key=True)


class SharedSelection(Base):
    """A persistent, unguessable link to an ordered selection of papers."""

    __tablename__ = "shared_selections"

    token: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )

    article_links: Mapped[list[SharedSelectionArticle]] = relationship(
        back_populates="selection",
        order_by="SharedSelectionArticle.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SharedSelectionArticle(Base):
    """One paper in a shared selection, retaining the creator's display order."""

    __tablename__ = "shared_selection_articles"
    __table_args__ = (
        UniqueConstraint("selection_token", "position", name="uq_shared_selection_position"),
    )

    selection_token: Mapped[str] = mapped_column(
        ForeignKey("shared_selections.token", ondelete="CASCADE"), primary_key=True
    )
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    selection: Mapped[SharedSelection] = relationship(back_populates="article_links")
    article: Mapped[Article] = relationship()
