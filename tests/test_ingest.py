from sqlalchemy import select


def _get_article(article_id: int):
    from app import db
    from app.models import Article

    with db.SessionLocal() as session:
        article = session.get(Article, article_id)
        article.authors  # force-load before session closes
        article.topics
        return article


def test_ingest_doi_runs_full_pipeline(client) -> None:
    resp = client.post("/api/ingest", data={"doi": "https://doi.org/10.1038/NPHYS1170"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "created"
    assert body["doi"] == "10.1038/nphys1170"

    # TestClient runs BackgroundTasks before returning, so metadata is applied.
    article = _get_article(body["article_id"])
    assert article.status == "ready"
    assert article.title == "Measured measurement"
    assert article.year == 2009
    assert article.journal == "Nature Physics"
    assert article.abstract == "Quantum measurement & its 100% weird consequences."
    assert [a.full_name for a in article.authors] == ["Alice B. Smith", "Carol Danvers"]
    assert article.authors[0].orcid == "0000-0002-1825-0097"
    assert [t.name for t in article.topics] == ["Physics and Astronomy"]


def test_hardcoded_name_alias_groups_nowak(client) -> None:
    """'Mateusz Marek Nowak' and 'Mateusz Nowak' must resolve to one canonical author."""
    from app import db, ingest

    with db.SessionLocal() as session:
        a = ingest._get_or_create_author(
            session, "Mateusz Marek Nowak", "0000-0002-8949-2720"
        )
        b = ingest._get_or_create_author(session, "Mateusz Nowak", None)
        session.commit()
        assert a.id == b.id
        assert a.full_name == "Mateusz Nowak"  # collapsed to the canonical name
        assert a.orcid == "0000-0002-8949-2720"  # ORCID preserved from the variant


def test_name_alias_is_applied_retroactively(client) -> None:
    """Authors stored before an alias existed must still collapse.

    Regression: _apply_name_alias only guards the write path, so a hard-coded alias
    looked like it did nothing against a library ingested earlier — the variants
    stayed as two rows, each with its own article.
    """
    from app import db, ingest
    from app.models import Article, ArticleAuthor, Author

    with db.SessionLocal() as session:
        # Simulate the pre-alias state: two separate rows, one article each.
        variant = Author(full_name="Mateusz Marek Nowak", orcid="0000-0002-8949-2720")
        canonical = Author(full_name="Mateusz Nowak")
        session.add_all([variant, canonical])
        session.flush()
        for author, doi in ((variant, "10.1/variant"), (canonical, "10.1/canonical")):
            article = Article(doi=doi, status="ready")
            session.add(article)
            session.flush()
            session.add(
                ArticleAuthor(article_id=article.id, author_id=author.id, position=0)
            )
        session.commit()
        variant_id, canonical_id = variant.id, canonical.id

    with db.SessionLocal() as session:
        assert ingest.apply_name_aliases(session) == 1

    with db.SessionLocal() as session:
        merged = session.get(Author, variant_id)
        target = session.get(Author, canonical_id)
        assert merged.merged_into_id == canonical_id  # soft merge, row still there
        assert target.orcid == "0000-0002-8949-2720"  # ORCID carried over
        articles = session.scalars(
            select(ArticleAuthor.article_id).where(
                ArticleAuthor.author_id == canonical_id
            )
        ).all()
        assert len(articles) == 2  # both papers now credited to one author


def test_name_alias_backfill_is_idempotent(client) -> None:
    from app import db, ingest
    from app.models import Author

    with db.SessionLocal() as session:
        session.add(Author(full_name="Mateusz Marek Nowak"))
        session.commit()

    with db.SessionLocal() as session:
        ingest.apply_name_aliases(session)
    with db.SessionLocal() as session:
        assert ingest.apply_name_aliases(session) == 0  # nothing left to merge


def test_ingest_duplicate_doi_is_soft(client) -> None:
    first = client.post("/api/ingest", data={"doi": "10.1038/nphys1170"}).json()
    second = client.post("/api/ingest", data={"doi": "doi:10.1038/NPHYS1170"})
    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "already_exists"
    assert body["article_id"] == first["article_id"]
    assert body["url"] == f"/articles/{first['article_id']}"

    from app import db
    from app.models import Article

    with db.SessionLocal() as session:
        count = len(session.scalars(select(Article)).all())
    assert count == 1


def test_ingest_without_doi_creates_stub_with_fuzzy_warning(client) -> None:
    client.post(
        "/api/ingest", data={"title": "Deep Learning for Cats and Dogs", "added_by": "michal"}
    )
    resp = client.post("/api/ingest", data={"title": "deep learning for cats and dogs!"})
    body = resp.json()
    assert body["status"] == "created"
    assert body["doi"] is None
    assert body["warnings"], "expected a possible_duplicate warning"
    assert body["warnings"][0]["type"] == "possible_duplicate"
    assert body["warnings"][0]["matches"][0]["title"] == "Deep Learning for Cats and Dogs"


def test_browser_form_post_redirects_to_article_page(client) -> None:
    resp = client.post(
        "/api/ingest",
        data={"doi": "10.1038/nphys1170"},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/articles/")

    page = client.get(location)
    assert page.status_code == 200
    assert "Added to the library" in page.text

    # duplicate submission from a browser redirects to the existing record
    resp = client.post(
        "/api/ingest",
        data={"doi": "10.1038/nphys1170"},
        headers={"accept": "text/html"},
        follow_redirects=True,
    )
    assert "already in the library" in resp.text


def test_ingest_rejects_empty_submission(client) -> None:
    resp = client.post("/api/ingest", data={"doi": "  "})
    assert resp.status_code == 422


def test_crossref_failure_marks_metadata_failed(client, monkeypatch) -> None:
    from app import ingest

    monkeypatch.setattr(ingest, "fetch_crossref", lambda doi: None)
    body = client.post("/api/ingest", data={"doi": "10.9999/gone"}).json()
    article = _get_article(body["article_id"])
    assert article.status == "metadata_failed"
