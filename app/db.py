import os
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base

DATA_DIR = Path(os.environ.get("REFBASE_DATA_DIR", "data"))
PDF_DIR = DATA_DIR / "pdfs"
DB_PATH = DATA_DIR / "refbase.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
PDF_DIR.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False}
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

# External-content FTS5 table kept in sync with `articles` via triggers.
_FTS_DDL = [
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
        title, abstract, journal,
        content='articles', content_rowid='id'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS articles_fts_ai AFTER INSERT ON articles BEGIN
        INSERT INTO articles_fts(rowid, title, abstract, journal)
        VALUES (new.id, new.title, new.abstract, new.journal);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS articles_fts_ad AFTER DELETE ON articles BEGIN
        INSERT INTO articles_fts(articles_fts, rowid, title, abstract, journal)
        VALUES ('delete', old.id, old.title, old.abstract, old.journal);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS articles_fts_au AFTER UPDATE ON articles BEGIN
        INSERT INTO articles_fts(articles_fts, rowid, title, abstract, journal)
        VALUES ('delete', old.id, old.title, old.abstract, old.journal);
        INSERT INTO articles_fts(rowid, title, abstract, journal)
        VALUES (new.id, new.title, new.abstract, new.journal);
    END
    """,
]


def init_db() -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for stmt in _FTS_DDL:
            conn.execute(text(stmt))


def search_article_ids(session: Session, query: str, limit: int = 50) -> list[int]:
    """Full-text search over title/abstract/journal; returns article ids ranked by bm25."""
    rows = session.execute(
        text(
            "SELECT rowid FROM articles_fts WHERE articles_fts MATCH :q "
            "ORDER BY rank LIMIT :limit"
        ),
        {"q": query, "limit": limit},
    )
    return [row[0] for row in rows]


def get_db():
    with SessionLocal() as session:
        yield session
