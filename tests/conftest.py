# Point the app at a throwaway data dir BEFORE anything imports app.db.
import os
import tempfile

os.environ["REFBASE_DATA_DIR"] = tempfile.mkdtemp(prefix="refbase-test-")

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def crossref_message() -> dict:
    return json.loads((FIXTURES / "crossref_sample.json").read_text())


@pytest.fixture()
def client(crossref_message, monkeypatch) -> TestClient:
    """TestClient on a clean database, with all external APIs mocked out."""
    from app import db, ingest
    from app.main import app
    from app.models import (
        Article,
        ArticleAuthor,
        ArticleKeyword,
        Author,
        Keyword,
        Project,
        ProjectDocument,
        SharedSelection,
        SharedSelectionArticle,
        Topic,
        TopicKeyword,
    )

    db.init_db()
    with db.SessionLocal() as session:
        project_files = session.scalars(
            select(ProjectDocument.file_path).where(
                ProjectDocument.file_path.is_not(None)
            )
        ).all()
        for model in (
            ProjectDocument,
            Project,
            SharedSelectionArticle,
            SharedSelection,
            ArticleAuthor,
            ArticleKeyword,
            TopicKeyword,
            Article,
            Author,
            Keyword,
            Topic,
        ):
            session.execute(delete(model))
        session.commit()
    for filename in project_files:
        Path(filename).unlink(missing_ok=True)

    monkeypatch.setattr(ingest, "fetch_crossref", lambda doi: crossref_message)
    monkeypatch.setattr(ingest, "fetch_semantic_scholar", lambda doi: None)
    monkeypatch.setattr(ingest, "fetch_unpaywall", lambda doi: None)

    return TestClient(app)
