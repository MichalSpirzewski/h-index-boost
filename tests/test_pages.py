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
    assert '<a href="/authors/1">Alex Adams</a>, ' in page
    assert '<a href="/authors/2">Alex Baker</a>, ' in page
    assert '<a href="/authors/3">Alex Clark</a> et al.' in page
    assert "Davis" not in page  # only the first three are shown


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
