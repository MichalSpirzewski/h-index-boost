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
    journal: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)
    crossref_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_by: Mapped[str | None] = mapped_column(String, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending/ready/metadata_failed
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
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

    article_links: Mapped[list[ArticleAuthor]] = relationship(back_populates="author")


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

    article: Mapped[Article] = relationship(back_populates="author_links")
    author: Mapped[Author] = relationship(back_populates="article_links")


class ArticleTopic(Base):
    __tablename__ = "article_topics"

    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"), primary_key=True)
