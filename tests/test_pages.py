import copy


def _ingest_with_authors(client, monkeypatch, base_message, doi, families):
    """Ingest one article whose Crossref response has the given author families."""
    from app import ingest

    message = copy.deepcopy(base_message)
    message["DOI"] = doi
    message["title"] = [f"Paper by {families[0]}"]
    message["author"] = [
        {"given": "Alex", "family": family, "sequence": "first" if i == 0 else "additional"}
        for i, family in enumerate(families)
    ]
    monkeypatch.setattr(ingest, "fetch_crossref", lambda _doi: message)
    resp = client.post("/api/ingest", data={"doi": doi})
    assert resp.json()["status"] == "created"


def test_dashboard_lists_first_three_authors_with_et_al(
    client, monkeypatch, crossref_message
) -> None:
    _ingest_with_authors(
        client, monkeypatch, crossref_message,
        "10.9999/many-authors", ["Adams", "Baker", "Clark", "Davis", "Evans"],
    )
    page = client.get("/").text
    # The article-table row is below the all-authors panel; scope the truncation
    # check to that row (the panel deliberately lists every unique author).
    table = page.split("</section>", 1)[-1]
    # The Authors column shows the shortened "A. Adams" form (full name in title=).
    assert '<a href="/authors/1" title="Alex Adams">A. Adams</a>, ' in table
    assert '<a href="/authors/2" title="Alex Baker">A. Baker</a>, ' in table
    assert '<a href="/authors/3" title="Alex Clark">A. Clark</a> et al.' in table
    assert "Davis" not in table  # only the first three show in the article row


def test_dashboard_sort_by_author(client, monkeypatch, crossref_message) -> None:
    # Zebra added first (most recent-last), Aardvark second (most recent-first)
    _ingest_with_authors(client, monkeypatch, crossref_message, "10.9999/z", ["Zebra"])
    _ingest_with_authors(client, monkeypatch, crossref_message, "10.9999/a", ["Aardvark"])

    recent = client.get("/").text
    assert recent.index("Aardvark") < recent.index("Zebra")  # newest first

    by_author = client.get("/?sort=author").text
    assert by_author.index("Aardvark") < by_author.index("Zebra")

    # reversed insertion order must still sort alphabetically
    _ingest_with_authors(client, monkeypatch, crossref_message, "10.9999/m", ["Miller"])
    by_author = client.get("/?sort=author").text
    assert (
        by_author.index("Aardvark") < by_author.index("Miller") < by_author.index("Zebra")
    )


def test_dashboard_filters_by_topic(client, monkeypatch, crossref_message) -> None:
    from sqlalchemy import select

    from app import db
    from app.models import ArticleTopic, Topic

    # Two papers tagged "Reactors", one tagged "Physics".
    def _ingest_with_subjects(doi, title, subjects):
        import copy

        from app import ingest

        message = copy.deepcopy(crossref_message)
        message["DOI"] = doi
        message["title"] = [title]
        message["subject"] = subjects
        monkeypatch.setattr(ingest, "fetch_crossref", lambda _doi: message)
        client.post("/api/ingest", data={"doi": doi})

    _ingest_with_subjects("10.7777/a", "Reactor Paper A", ["Reactors"])
    _ingest_with_subjects("10.7777/b", "Reactor Paper B", ["Reactors"])
    _ingest_with_subjects("10.7777/c", "Physics Paper C", ["Physics"])

    with db.SessionLocal() as session:
        reactors_id = session.scalar(select(Topic.id).where(Topic.name == "Reactors"))
        # sanity: 2 articles carry it
        tagged = session.scalars(
            select(ArticleTopic.article_id).where(ArticleTopic.topic_id == reactors_id)
        ).all()
        assert len(tagged) == 2

    page = client.get(f"/?topic={reactors_id}").text
    assert "Papers tagged" in page
    assert "Reactor Paper A" in page and "Reactor Paper B" in page
    assert "Physics Paper C" not in page  # filtered out


def test_author_page_lists_only_that_authors_articles(
    client, monkeypatch, crossref_message
) -> None:
    _ingest_with_authors(
        client, monkeypatch, crossref_message, "10.9999/shared", ["Shared", "Solo"],
    )
    _ingest_with_authors(
        client, monkeypatch, crossref_message, "10.9999/other", ["Other"],
    )

    page = client.get("/authors/1").text
    assert "Alex Shared" in page
    assert "Paper by Shared" in page
    assert "Paper by Other" not in page  # not this author's article

    # co-author from the shared paper should be linked, unrelated author should not appear
    assert '<a href="/authors/2">Alex Solo</a>' in page
    assert "Alex Other" not in page


def test_author_page_filters_publications_by_topic(client, monkeypatch, crossref_message) -> None:
    import copy

    from sqlalchemy import select

    from app import db, ingest
    from app.models import Author, Topic

    def _ingest(doi, title, subjects):
        message = copy.deepcopy(crossref_message)
        message["DOI"] = doi
        message["title"] = [title]
        message["subject"] = subjects
        message["author"] = [{"given": "Ada", "family": "Kowalska", "sequence": "first"}]
        monkeypatch.setattr(ingest, "fetch_crossref", lambda _doi: message)
        client.post("/api/ingest", data={"doi": doi})

    _ingest("10.8888/a", "Reactor Study", ["Reactors"])
    _ingest("10.8888/b", "Physics Study", ["Physics"])

    with db.SessionLocal() as session:
        author_id = session.scalar(select(Author.id).where(Author.full_name == "Ada Kowalska"))
        reactors_id = session.scalar(select(Topic.id).where(Topic.name == "Reactors"))

    # Unfiltered: both papers shown; the filter stays on the author page (not the dashboard).
    full = client.get(f"/authors/{author_id}").text
    assert "Reactor Study" in full and "Physics Study" in full
    assert f'href="/authors/{author_id}?topic={reactors_id}"' in full

    filtered = client.get(f"/authors/{author_id}?topic={reactors_id}").text
    assert "Publications tagged" in filtered
    assert "Reactor Study" in filtered
    assert "Physics Study" not in filtered  # filtered out, in-place


def test_author_page_404_for_unknown_author(client) -> None:
    resp = client.get("/authors/999")
    assert resp.status_code == 404


def test_dashboard_shows_pdf_actions_only_when_pdf_attached(client, monkeypatch) -> None:
    from app import ingest

    monkeypatch.setattr(ingest, "fetch_crossref", lambda _doi: None)
    with_pdf = client.post(
        "/api/ingest",
        data={"doi": "10.9999/has-pdf"},
        files={"file": ("paper.pdf", b"%PDF-1.4 fake pdf bytes", "application/pdf")},
    ).json()
    without_pdf = client.post("/api/ingest", data={"doi": "10.9999/no-pdf"}).json()

    page = client.get("/").text
    assert f'href="/articles/{with_pdf["article_id"]}/pdf"' in page
    assert f'href="/articles/{with_pdf["article_id"]}/pdf?download=1"' in page
    assert f'/articles/{without_pdf["article_id"]}/pdf"' not in page


def test_article_pdf_view_is_inline_download_is_attachment(client) -> None:
    body = client.post(
        "/api/ingest",
        data={"doi": "10.1038/nphys1170"},
        files={"file": ("paper.pdf", b"%PDF-1.4 fake pdf bytes", "application/pdf")},
    ).json()
    article_id = body["article_id"]

    view = client.get(f"/articles/{article_id}/pdf")
    assert view.status_code == 200
    assert view.headers["content-disposition"].startswith("inline;")

    download = client.get(f"/articles/{article_id}/pdf?download=1")
    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment;")


def test_article_page_offers_a_word_download(client) -> None:
    article_id = client.post("/api/ingest", data={"doi": "10.1038/nphys1170"}).json()["article_id"]

    page = client.get(f"/articles/{article_id}").text
    assert f'href="/articles/{article_id}/word-xml"' in page

    resp = client.get(f"/articles/{article_id}/word-xml")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    assert 'filename="smith2009measured.xml"' in resp.headers["content-disposition"]
    assert "<b:Tag>smith2009measured</b:Tag>" in resp.text


def test_word_xml_404_for_unknown_article(client) -> None:
    assert client.get("/articles/999/word-xml").status_code == 404
