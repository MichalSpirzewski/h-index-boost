from sqlalchemy import text


def _hit_ids(session, query):
    from app import db

    return [article_id for article_id, _ in db.search_articles(session, query)]


def test_wal_mode_enabled(client) -> None:
    from app import db

    with db.engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"


def test_fts5_search_stays_in_sync(client) -> None:
    from app import db
    from app.models import Article

    with db.SessionLocal() as session:
        article = Article(title="Zeolite catalysis review", journal="ACS Catalysis", status="ready")
        session.add(article)
        session.commit()
        assert _hit_ids(session, "zeolite") == [article.id]

        article.title = "Perovskite solar cells"
        session.commit()
        assert _hit_ids(session, "zeolite") == []
        assert _hit_ids(session, "perovskite") == [article.id]

        session.delete(article)
        session.commit()
        assert _hit_ids(session, "perovskite") == []


def test_search_covers_pdf_text_with_marked_snippet(client) -> None:
    from app import db
    from app.models import Article

    with db.SessionLocal() as session:
        article = Article(
            title="Reactor Physics Handbook",
            pdf_text="Chapter 3 discusses the neutron flux spectrum in detail.",
            status="ready",
        )
        session.add(article)
        session.commit()

        results = db.search_articles(session, "neutron flux")
        assert [article_id for article_id, _ in results] == [article.id]
        snippet = str(results[0][1])
        assert "<mark>neutron</mark>" in snippet
        assert "<mark>flux</mark>" in snippet


def test_fts_special_characters_do_not_crash(client) -> None:
    from app import db

    with db.SessionLocal() as session:
        assert db.search_articles(session, 'AND OR "quoted" near(') == []
        assert db.search_articles(session, "   ") == []


def test_search_page_finds_text_inside_uploaded_pdf(client) -> None:
    import fitz

    doc = fitz.open()
    doc.new_page().insert_text(
        (72, 72), "This report covers molten salt corrosion loops."
    )
    resp = client.post(
        "/api/ingest",
        data={"title": "Salt Loop Report"},
        files={"file": ("report.pdf", doc.tobytes(), "application/pdf")},
    )
    assert resp.json()["status"] == "created"

    page = client.get("/search", params={"q": "molten salt corrosion"})
    assert "Salt Loop Report" in page.text
    assert "<mark>molten</mark>" in page.text
