import os
import re
from pathlib import Path

from markupsafe import Markup, escape
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base

DATA_DIR = Path(os.environ.get("REFBASE_DATA_DIR", "data"))
PDF_DIR = DATA_DIR / "pdfs"
PROJECT_DOCUMENT_DIR = DATA_DIR / "project_documents"
DB_PATH = DATA_DIR / "refbase.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
PDF_DIR.mkdir(parents=True, exist_ok=True)
PROJECT_DOCUMENT_DIR.mkdir(parents=True, exist_ok=True)

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


def _rename_legacy_keyword_tables(conn) -> None:
    """`topics`/`article_topics` used to hold what is now called keywords.

    Topics became the layer *above* keywords, so the old tables are renamed and
    the freed-up `topics` name goes to the new grouping model. Runs before
    create_all, or the new empty `topics` table would block the rename.

    The legacy layout is recognised by `article_topics` — a table the current
    schema never creates.
    """
    tables = {
        row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
    }
    if "article_topics" not in tables:  # fresh database, or already migrated
        return

    def row_count(table: str) -> int:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0

    # create_all may have made the new tables already — it does whenever the
    # models reach a deployment before this migration does. They are empty in
    # that case and have to go before the legacy tables can take their names.
    # Children first: each references the table after it.
    for table in ("topic_keywords", "article_keywords", "keywords"):
        if table in tables:
            if row_count(table):  # a populated new schema: nothing to move
                return
            conn.execute(text(f"DROP TABLE {table}"))

    conn.execute(text("ALTER TABLE topics RENAME TO keywords"))
    # SQLite rewrites the child table's foreign key on the rename above, so only
    # the column name is left to fix.
    conn.execute(text("ALTER TABLE article_topics RENAME TO article_keywords"))
    conn.execute(
        text("ALTER TABLE article_keywords RENAME COLUMN topic_id TO keyword_id")
    )


def init_db() -> None:
    with engine.begin() as conn:
        _rename_legacy_keyword_tables(conn)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        # Lightweight in-place migration for databases created before pdf_text existed.
        article_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(articles)"))]
        if article_cols and "pdf_text" not in article_cols:
            conn.execute(text("ALTER TABLE articles ADD COLUMN pdf_text TEXT"))
        if article_cols and "pdf_sha256" not in article_cols:
            conn.execute(text("ALTER TABLE articles ADD COLUMN pdf_sha256 VARCHAR(64)"))
        # Deliberately not UNIQUE: records already duplicated in a library share a
        # hash, and hidden copies keep theirs.
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_articles_pdf_sha256 "
                "ON articles(pdf_sha256)"
            )
        )
        if article_cols and "cite_first" not in article_cols:
            conn.execute(
                text("ALTER TABLE articles ADD COLUMN cite_first BOOLEAN DEFAULT 0")
            )
        if article_cols and "citation_examples" not in article_cols:
            conn.execute(text("ALTER TABLE articles ADD COLUMN citation_examples TEXT"))
        if article_cols and "highlights" not in article_cols:
            conn.execute(text("ALTER TABLE articles ADD COLUMN highlights TEXT"))
        if article_cols and "publication_type" not in article_cols:
            # All pre-hierarchy records came through the journal-paper workflow.
            conn.execute(
                text(
                    "ALTER TABLE articles ADD COLUMN publication_type "
                    "VARCHAR NOT NULL DEFAULT 'journal'"
                )
            )

        for column in ("published_date", "online_date"):
            if article_cols and column not in article_cols:
                conn.execute(text(f"ALTER TABLE articles ADD COLUMN {column} VARCHAR"))
        for column in ("conference_start_date", "conference_end_date"):
            if article_cols and column not in article_cols:
                conn.execute(text(f"ALTER TABLE articles ADD COLUMN {column} VARCHAR"))
        for column in ("conference_name", "proceedings_title", "conference_location"):
            if article_cols and column not in article_cols:
                conn.execute(text(f"ALTER TABLE articles ADD COLUMN {column} TEXT"))

        author_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(authors)"))]
        if author_cols and "merged_into_id" not in author_cols:
            conn.execute(text("ALTER TABLE authors ADD COLUMN merged_into_id INTEGER"))

        aa_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(article_authors)"))]
        if aa_cols and "affiliation" not in aa_cols:
            conn.execute(text("ALTER TABLE article_authors ADD COLUMN affiliation TEXT"))

        project_doc_cols = [
            row[1] for row in conn.execute(text("PRAGMA table_info(project_documents)"))
        ]
        for column, sql_type in (
            ("local_project_id", "VARCHAR"),
            ("lead_beneficiary", "TEXT"),
            ("authors_json", "TEXT"),
            ("published_date", "VARCHAR"),
            ("pdf_text", "TEXT"),
            ("parse_status", "VARCHAR NOT NULL DEFAULT 'not_parsed'"),
            ("parse_warnings", "TEXT"),
            ("original_filename", "TEXT"),
            ("file_path", "TEXT"),
            ("mime_type", "VARCHAR"),
            ("byte_size", "INTEGER"),
        ):
            if project_doc_cols and column not in project_doc_cols:
                conn.execute(
                    text(
                        f"ALTER TABLE project_documents ADD COLUMN {column} {sql_type}"
                    )
                )

        project_cols = [
            row[1] for row in conn.execute(text("PRAGMA table_info(projects)"))
        ]
        if project_cols and "project_number" not in project_cols:
            conn.execute(text("ALTER TABLE projects ADD COLUMN project_number VARCHAR"))
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_projects_project_number "
                "ON projects(project_number) WHERE project_number IS NOT NULL"
            )
        )

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


def count_search_matches(query: str, *texts: str | None) -> int:
    """Count occurrences of each searched term across the FTS-indexed fields."""
    terms = [term.casefold() for term in re.findall(r"\w+", query, re.UNICODE)]
    content = "\n".join(text or "" for text in texts).casefold()
    return sum(content.count(term) for term in terms)


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
