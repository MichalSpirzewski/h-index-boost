from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(primary_key=True)
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
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-form examples suggested by library users for citing this publication.
    citation_examples: Mapped[str | None] = mapped_column(Text, nullable=True)
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
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    author_links: Mapped[list[ArticleAuthor]] = relationship(
        back_populates="article",
        order_by="ArticleAuthor.position",
        cascade="all, delete-orphan",
    )
    topics: Mapped[list[Topic]] = relationship(
        secondary="article_topics", back_populates="articles"
    )

    @property
    def authors(self) -> list[Author]:
        return [link.author for link in self.author_links]


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
