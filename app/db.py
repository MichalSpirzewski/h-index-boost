import os
from pathlib import Path

from markupsafe import Markup, escape
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
_FTS_COLUMNS = ("title", "abstract", "journal", "pdf_text")
_FTS_DDL = [
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
        title, abstract, journal, pdf_text,
        content='articles', content_rowid='id'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS articles_fts_ai AFTER INSERT ON articles BEGIN
        INSERT INTO articles_fts(rowid, title, abstract, journal, pdf_text)
        VALUES (new.id, new.title, new.abstract, new.journal, new.pdf_text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS articles_fts_ad AFTER DELETE ON articles BEGIN
        INSERT INTO articles_fts(articles_fts, rowid, title, abstract, journal, pdf_text)
        VALUES ('delete', old.id, old.title, old.abstract, old.journal, old.pdf_text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS articles_fts_au AFTER UPDATE ON articles BEGIN
        INSERT INTO articles_fts(articles_fts, rowid, title, abstract, journal, pdf_text)
        VALUES ('delete', old.id, old.title, old.abstract, old.journal, old.pdf_text);
        INSERT INTO articles_fts(rowid, title, abstract, journal, pdf_text)
        VALUES (new.id, new.title, new.abstract, new.journal, new.pdf_text);
    END
    """,
]


def init_db() -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        # Lightweight in-place migration for databases created before pdf_text existed.
        article_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(articles)"))]
        if article_cols and "pdf_text" not in article_cols:
            conn.execute(text("ALTER TABLE articles ADD COLUMN pdf_text TEXT"))

        for column in ("published_date", "online_date"):
            if article_cols and column not in article_cols:
                conn.execute(text(f"ALTER TABLE articles ADD COLUMN {column} VARCHAR"))

        author_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(authors)"))]
        if author_cols and "merged_into_id" not in author_cols:
            conn.execute(text("ALTER TABLE authors ADD COLUMN merged_into_id INTEGER"))

        aa_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(article_authors)"))]
        if aa_cols and "affiliation" not in aa_cols:
            conn.execute(text("ALTER TABLE article_authors ADD COLUMN affiliation TEXT"))

        fts_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(articles_fts)"))]
        rebuild = bool(fts_cols) and set(fts_cols) != set(_FTS_COLUMNS)
        if rebuild:
            conn.execute(text("DROP TABLE articles_fts"))
            for trigger in ("articles_fts_ai", "articles_fts_ad", "articles_fts_au"):
                conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger}"))

        for stmt in _FTS_DDL:
            conn.execute(text(stmt))
        if rebuild:
            conn.execute(text("INSERT INTO articles_fts(articles_fts) VALUES('rebuild')"))


def _fts_match_expr(query: str) -> str:
    """Quote each term as a phrase so user input can't break FTS5 query syntax."""
    terms = [term.replace('"', "") for term in query.split()]
    return " ".join(f'"{term}"' for term in terms if term)


def search_articles(session: Session, query: str, limit: int = 50) -> list[tuple[int, Markup]]:
    """FTS5 search over title/abstract/journal/pdf_text.

    Returns (article_id, snippet) pairs ranked by bm25; snippets are
    HTML-escaped with matches wrapped in <mark>.
    """
    match = _fts_match_expr(query)
    if not match:
        return []
    rows = session.execute(
        text(
            "SELECT rowid, snippet(articles_fts, -1, char(2), char(3), ' … ', 12) "
            "FROM articles_fts WHERE articles_fts MATCH :q ORDER BY rank LIMIT :limit"
        ),
        {"q": match, "limit": limit},
    )
    return [
        (
            row[0],
            Markup(str(escape(row[1])).replace("\x02", "<mark>").replace("\x03", "</mark>")),
        )
        for row in rows
    ]


def get_db():
    with SessionLocal() as session:
        yield session
