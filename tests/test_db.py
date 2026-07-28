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


def test_search_match_count_covers_all_indexed_fields() -> None:
    from app import db

    assert (
        db.count_search_matches(
            "reactor safety",
            "Reactor safety study",
            "The reactor improves SAFETY and reactor reliability.",
            "Nuclear Safety",
            None,
        )
        == 6
    )


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
    assert 'class="wide-table search-results-table"' in page.text
    assert ">Count</th>" in page.text
    assert '<td class="search-count">3</td>' in page.text
    assert 'formaction="/export/bibtex"' in page.text
    assert ">Cite first</th>" in page.text
    assert ">PDF</th>" in page.text


def test_search_results_can_be_sorted_by_headings(client) -> None:
    from app import db
    from app.models import Article

    with db.SessionLocal() as session:
        session.add_all(
            [
                Article(title="Zebra reactor reactor", status="ready"),
                Article(title="Alpha reactor", status="pending"),
            ]
        )
        session.commit()

    by_title = client.get(
        "/search", params={"q": "reactor", "sort": "title", "order": "asc"}
    ).text
    assert by_title.index("Alpha reactor") < by_title.index("Zebra reactor reactor")
    assert "sort=title&amp;order=desc" in by_title
    assert '<span class="arrow">▲</span>' in by_title
    assert "sort=count" in by_title

    by_count = client.get(
        "/search", params={"q": "reactor", "sort": "count", "order": "desc"}
    ).text
    assert by_count.index("Zebra reactor reactor") < by_count.index("Alpha reactor")
    assert "sort=count&amp;order=asc" in by_count
    assert '<span class="arrow">▼</span>' in by_count
    assert "<mark>molten</mark>" in page.text
