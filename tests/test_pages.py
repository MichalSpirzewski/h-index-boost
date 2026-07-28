import copy
import io
import zipfile


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


def test_journal_links_open_page_with_only_that_journals_publications(
    client, monkeypatch, crossref_message
) -> None:
    from app import ingest

    for doi, title, journal in (
        ("10.9999/journal-a", "Journal Paper A", "Journal of Tests"),
        ("10.9999/journal-b", "Journal Paper B", "Other Review"),
    ):
        message = copy.deepcopy(crossref_message)
        message["DOI"] = doi
        message["title"] = [title]
        message["container-title"] = [journal]
        monkeypatch.setattr(ingest, "fetch_crossref", lambda _doi, m=message: m)
        client.post("/api/ingest", data={"doi": doi})

    dashboard = client.get("/").text
    assert 'href="/journals/Journal%20of%20Tests"' in dashboard

    page = client.get("/journals/Journal%20of%20Tests").text
    assert "<h1>Journal of Tests</h1>" in page
    assert "Journal Paper A" in page
    assert "Journal Paper B" not in page


def test_journal_page_404_for_unknown_journal(client) -> None:
    assert client.get("/journals/Unknown%20Journal").status_code == 404


def test_author_and_journal_tables_match_dashboard_actions(client) -> None:
    body = client.post(
        "/api/ingest",
        data={"doi": "10.1038/nphys1170"},
        files={"file": ("paper.pdf", b"%PDF-1.4 fake pdf bytes", "application/pdf")},
    ).json()
    article_id = body["article_id"]

    for url in ("/authors/1", "/journals/Nature%20Physics"):
        page = client.get(url).text
        assert ">Authors<" in page
        assert ">Status<" in page
        assert ">Cite first<" in page
        assert ">PDF<" in page
        assert f'href="/articles/{article_id}/pdf"' in page
        assert f'href="/articles/{article_id}/pdf?download=1"' in page
        assert f'formaction="/articles/{article_id}/cite-first"' in page

    response = client.post(
        f"/articles/{article_id}/cite-first",
        data={"return_to": "/authors/1?sort=year&order=desc"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/authors/1?sort=year&order=desc"


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


def test_cite_first_toggle_marks_and_unmarks_article(client) -> None:
    article_id = client.post("/api/ingest", data={"doi": "10.9999/cite-first"}).json()["article_id"]

    page = client.get("/").text
    assert 'cite-first-active' not in page

    # Mirrors what the dashboard form submits on its default (newest-published) view.
    default_view = {"sort": "year", "order": "desc"}

    marked = client.post(f"/articles/{article_id}/cite-first", data=default_view)
    assert marked.status_code == 200  # redirect followed
    assert 'cite-first-active' in marked.text

    unmarked = client.post(f"/articles/{article_id}/cite-first", data=default_view)
    assert 'cite-first-active' not in unmarked.text


def test_cite_first_toggle_preserves_the_active_sort(client) -> None:
    """The redirect must land back on the view the user was looking at, not the default."""
    article_id = client.post("/api/ingest", data={"doi": "10.9999/cite-sort"}).json()["article_id"]

    resp = client.post(
        f"/articles/{article_id}/cite-first",
        data={"sort": "recent", "order": "desc"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/?sort=recent&order=desc"


def test_cite_first_toggle_404_for_unknown_article(client) -> None:
    resp = client.post("/articles/999/cite-first", data={"sort": "year", "order": "desc"})
    assert resp.status_code == 404


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


def test_article_page_can_save_citation_examples(client) -> None:
    article_id = client.post(
        "/api/ingest", data={"doi": "10.9999/citation-examples"}
    ).json()["article_id"]

    page = client.get(f"/articles/{article_id}").text
    assert (
        'placeholder="Dodaj tutaj 3-4 przykłady jak ten artykuł mógłby być '
        'cytowany w innych pracach"'
    ) in page

    example = "Metoda została zastosowana do analizy bezpieczeństwa reaktora."
    response = client.post(
        f"/articles/{article_id}/citation-examples",
        data={"citation_examples": example},
    )

    assert response.status_code == 200
    assert "Zapisano przykłady cytowania." in response.text
    assert example in client.get(f"/articles/{article_id}").text


def test_article_page_can_fetch_publisher_highlights_once(
    client, monkeypatch
) -> None:
    from app import ingest

    article_id = client.post(
        "/api/ingest", data={"doi": "10.9999/highlights"}
    ).json()["article_id"]
    page = client.get(f"/articles/{article_id}").text
    assert f'action="/articles/{article_id}/fetch-highlights"' in page

    calls = []

    def fake_fetch(doi):
        calls.append(doi)
        return ["First highlight.", "Second highlight."]

    monkeypatch.setattr(ingest, "fetch_publisher_highlights", fake_fetch)
    response = client.post(f"/articles/{article_id}/fetch-highlights")

    assert response.status_code == 200
    assert "Publisher Highlights saved." in response.text
    assert "First highlight.\nSecond highlight." in response.text
    assert "Fetch Highlights" not in response.text
    assert calls == ["10.9999/highlights"]

    client.post(f"/articles/{article_id}/fetch-highlights")
    assert calls == ["10.9999/highlights"]  # stored result is not fetched twice


def test_article_page_can_save_highlights_manually(client) -> None:
    article_id = client.post(
        "/api/ingest", data={"doi": "10.1038/nphys1170"}
    ).json()["article_id"]
    highlights = "First manual highlight.\nSecond manual highlight."

    response = client.post(
        f"/articles/{article_id}/highlights",
        data={"highlights": highlights},
    )

    assert response.status_code == 200
    assert "Zapisano Highlights." in response.text
    assert highlights in response.text
    assert response.text.index("Przykłady cytowania") < response.text.index("Abstract")
    assert response.text.index("Highlights") < response.text.index("Abstract")


def test_saving_citation_examples_for_unknown_article_returns_404(client) -> None:
    response = client.post(
        "/articles/999/citation-examples",
        data={"citation_examples": "Example"},
    )
    assert response.status_code == 404


def test_word_xml_404_for_unknown_article(client) -> None:
    assert client.get("/articles/999/word-xml").status_code == 404


def _two_articles(client, monkeypatch, crossref_message):
    """Ingest two articles with distinct first authors; return their ids in order."""
    from sqlalchemy import select

    from app import db
    from app.models import Article

    for doi, family in (("10.9999/first", "Adams"), ("10.9999/second", "Baker")):
        _ingest_with_authors(client, monkeypatch, crossref_message, doi, [family])
    with db.SessionLocal() as session:
        return list(session.scalars(select(Article.id).order_by(Article.id)))


def test_dashboard_panels_are_collapsed_by_default(client, monkeypatch, crossref_message) -> None:
    _ingest_with_authors(client, monkeypatch, crossref_message, "10.9999/panels", ["Adams"])
    page = client.get("/").text
    # The Authors / Keywords panels fold; the table sections below them do not.
    # <details> with no `open` attribute renders folded.
    for panel in ("Authors", "Keywords &amp; topics"):
        summary = page.index(f'<span class="panel-title">{panel}</span>')
        assert page.rindex("<details", 0, summary) == page.rindex("<details>", 0, summary)


def test_topic_panel_opens_when_a_topic_filter_is_active(
    client, monkeypatch, crossref_message
) -> None:
    _ingest_with_authors(client, monkeypatch, crossref_message, "10.9999/topicopen", ["Adams"])
    from app import db
    from app.models import Topic

    with db.SessionLocal() as session:
        topic_id = session.query(Topic).first().id
    assert "<details open>" in client.get(f"/?topic={topic_id}").text


def test_dashboard_and_author_page_offer_multi_select_export(
    client, monkeypatch, crossref_message
) -> None:
    _ingest_with_authors(client, monkeypatch, crossref_message, "10.9999/multi", ["Adams"])
    for url in ("/", "/authors/1"):
        page = client.get(url)
        assert page.status_code == 200
        assert 'class="js-export-form"' in page.text
        assert 'formaction="/export/bibtex"' in page.text
        assert 'formaction="/export/word-xml"' in page.text
        assert 'formaction="/export/pdfs"' in page.text
        assert 'formaction="/export/all"' in page.text
        assert 'name="ids"' in page.text


def test_merged_bibtex_export_of_selected_ids(client, monkeypatch, crossref_message) -> None:
    ids = _two_articles(client, monkeypatch, crossref_message)

    resp = client.post("/export/bibtex", data={"ids": ids})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-bibtex")
    assert f'filename="refbase-{len(ids)}-refs.bib"' in resp.headers["content-disposition"]
    assert resp.text.count("@article{") == len(ids)
    assert "adams" in resp.text and "baker" in resp.text


def test_merged_word_export_of_selected_ids(client, monkeypatch, crossref_message) -> None:
    ids = _two_articles(client, monkeypatch, crossref_message)

    resp = client.post("/export/word-xml", data={"ids": ids})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    assert resp.text.count("<b:Source>") == len(ids)


def test_selected_pdfs_and_all_formats_download_as_zip(
    client, monkeypatch, crossref_message
) -> None:
    from app import ingest

    ids = []
    for index, family in enumerate(("Adams", "Baker"), start=1):
        message = copy.deepcopy(crossref_message)
        message["DOI"] = f"10.9999/zip-{index}"
        message["title"] = [f"ZIP paper {index}"]
        message["author"] = [
            {"given": "Alex", "family": family, "sequence": "first"}
        ]
        monkeypatch.setattr(ingest, "fetch_crossref", lambda _doi, m=message: m)
        body = client.post(
            "/api/ingest",
            data={"doi": message["DOI"]},
            files={
                "file": (
                    f"paper-{index}.pdf",
                    b"%PDF-1.4 selected test PDF",
                    "application/pdf",
                )
            },
        ).json()
        ids.append(body["article_id"])

    pdf_response = client.post("/export/pdfs", data={"ids": ids})
    assert pdf_response.status_code == 200
    assert pdf_response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(pdf_response.content)) as archive:
        pdf_members = [name for name in archive.namelist() if name.endswith(".pdf")]
        assert len(pdf_members) == 2

    all_response = client.post("/export/all", data={"ids": ids})
    assert all_response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(all_response.content)) as archive:
        names = archive.namelist()
        assert "refbase-selected.bib" in names
        assert "refbase-selected.xml" in names
        assert len([name for name in names if name.endswith(".pdf")]) == 2
        assert archive.read("refbase-selected.bib").count(b"@article{") == 2
        assert archive.read("refbase-selected.xml").count(b"<b:Source>") == 2


def test_export_of_a_subset_only_includes_that_subset(
    client, monkeypatch, crossref_message
) -> None:
    ids = _two_articles(client, monkeypatch, crossref_message)

    resp = client.post("/export/bibtex", data={"ids": [ids[0]]})
    assert resp.text.count("@article{") == 1
    assert "baker" not in resp.text


def test_export_with_nothing_selected_is_a_clean_400(client) -> None:
    assert client.post("/export/bibtex", data={}).status_code == 400
    assert client.post("/export/word-xml", data={}).status_code == 400


def test_export_ignores_hidden_and_unknown_ids(client, monkeypatch, crossref_message) -> None:
    ids = _two_articles(client, monkeypatch, crossref_message)
    from app import db
    from app.models import Article

    with db.SessionLocal() as session:
        session.get(Article, ids[1]).hidden = True
        session.commit()

    resp = client.post("/export/bibtex", data={"ids": ids + [99999]})
    assert resp.status_code == 200
    assert resp.text.count("@article{") == 1  # hidden and unknown ids dropped


def _ingest_with_year(client, monkeypatch, base_message, doi, year):
    """Ingest one article published in `year`."""
    from app import ingest

    message = copy.deepcopy(base_message)
    message["DOI"] = doi
    message["title"] = [f"Paper from {year}"]
    message["issued"] = {"date-parts": [[year, 1]]}
    monkeypatch.setattr(ingest, "fetch_crossref", lambda _doi: message)
    assert client.post("/api/ingest", data={"doi": doi}).json()["status"] == "created"


def test_dashboard_defaults_to_newest_published_not_newest_uploaded(
    client, monkeypatch, crossref_message
) -> None:
    # Upload order is deliberately the reverse of publication order.
    for i, year in enumerate((2019, 2025, 2021)):
        _ingest_with_year(client, monkeypatch, crossref_message, f"10.9999/y{i}", year)

    page = client.get("/").text
    positions = [page.index(f"Paper from {y}") for y in (2025, 2021, 2019)]
    assert positions == sorted(positions), "expected newest publication first"
    assert "Latest publications" in page


def test_dashboard_can_still_sort_by_date_added(client, monkeypatch, crossref_message) -> None:
    for i, year in enumerate((2019, 2025, 2021)):
        _ingest_with_year(client, monkeypatch, crossref_message, f"10.9999/added{i}", year)

    page = client.get("/?sort=recent").text
    assert "Recently added" in page
    # Last uploaded (2021) comes first, regardless of publication year.
    positions = [page.index(f"Paper from {y}") for y in (2021, 2025, 2019)]
    assert positions == sorted(positions)


def test_author_page_publications_default_to_newest_published(
    client, monkeypatch, crossref_message
) -> None:
    for i, year in enumerate((2018, 2026, 2020)):
        message = copy.deepcopy(crossref_message)
        message["DOI"] = f"10.9999/auth{i}"
        message["title"] = [f"Paper from {year}"]
        message["issued"] = {"date-parts": [[year, 1]]}
        message["author"] = [{"given": "Alex", "family": "Adams", "sequence": "first"}]
        from app import ingest

        monkeypatch.setattr(ingest, "fetch_crossref", lambda _doi, m=message: m)
        client.post("/api/ingest", data={"doi": f"10.9999/auth{i}"})

    page = client.get("/authors/1").text
    positions = [page.index(f"Paper from {y}") for y in (2026, 2020, 2018)]
    assert positions == sorted(positions)


def test_articles_without_a_year_sort_last_not_first(
    client, monkeypatch, crossref_message
) -> None:
    _ingest_with_year(client, monkeypatch, crossref_message, "10.9999/dated", 2020)
    # A DOI-less stub never gets a year from Crossref.
    client.post("/api/ingest", data={"title": "Undated Stub Paper"})

    page = client.get("/").text
    assert page.index("Paper from 2020") < page.index("Undated Stub Paper")


def _ingest_with_dates(client, monkeypatch, base_message, doi, issued, created=None):
    """Ingest one article with an explicit Crossref issue date."""
    from app import ingest

    message = copy.deepcopy(base_message)
    message["DOI"] = doi
    message["title"] = [f"Paper {'-'.join(str(p) for p in issued)}"]
    message["issued"] = {"date-parts": [issued]}
    if created:
        message["created"] = {"date-parts": [created]}
    monkeypatch.setattr(ingest, "fetch_crossref", lambda _doi: message)
    client.post("/api/ingest", data={"doi": doi})


def test_papers_from_one_year_order_by_month(client, monkeypatch, crossref_message) -> None:
    """The point of storing more than the year: 2026 papers no longer all tie."""
    for i, issued in enumerate(([2026, 4], [2026, 12], [2026, 5])):
        _ingest_with_dates(client, monkeypatch, crossref_message, f"10.9999/m{i}", issued)

    page = client.get("/").text
    order = [page.index(f"Paper 2026-{m}") for m in (12, 5, 4)]
    assert order == sorted(order), "expected December before May before April"


def test_partial_dates_render_without_an_invented_day(
    client, monkeypatch, crossref_message
) -> None:
    _ingest_with_dates(
        client, monkeypatch, crossref_message, "10.9999/partial", [2026, 5], [2026, 5, 21]
    )
    page = client.get("/").text
    assert ">2026-05<" in page  # month precision kept as-is
    assert ">2026-05-01<" not in page  # no fabricated first-of-month
    assert ">2026-05-21<" in page  # the online date does have a day


def test_online_column_is_sortable_separately(client, monkeypatch, crossref_message) -> None:
    _ingest_with_dates(
        client, monkeypatch, crossref_message, "10.9999/o1", [2026, 12], [2026, 7, 1]
    )
    _ingest_with_dates(
        client, monkeypatch, crossref_message, "10.9999/o2", [2026, 4], [2026, 11, 3]
    )
    # By issue date the December paper leads; by online date the November one does.
    by_issue = client.get("/").text
    assert by_issue.index("Paper 2026-12") < by_issue.index("Paper 2026-4")
    by_online = client.get("/?sort=online").text
    assert by_online.index("Paper 2026-4") < by_online.index("Paper 2026-12")


def test_export_form_never_posts_back_to_the_page_itself(
    client, monkeypatch, crossref_message
) -> None:
    """Regression: the form had no action, so an implicit submit with the buttons
    disabled posted to '/' — which serves GET only — and returned a raw 405."""
    _ingest_with_authors(client, monkeypatch, crossref_message, "10.9999/405", ["Adams"])

    for url in ("/", "/authors/1"):
        page = client.get(url).text
        assert 'action="/export/bibtex"' in page
        # The page itself must reject POST; the form must not be aimed at it.
        assert client.post(url).status_code == 405


def test_browser_posting_an_empty_selection_is_sent_back_not_shown_json(client) -> None:
    resp = client.post(
        "/export/bibtex",
        data={},
        headers={"accept": "text/html", "referer": "http://testserver/authors/1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "http://testserver/authors/1"

    # Same request without a referer still lands somewhere sensible.
    resp = client.post(
        "/export/word-xml", data={}, headers={"accept": "text/html"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_cite_first_button_is_not_a_nested_form(client, monkeypatch, crossref_message) -> None:
    """Regression: the toggle used its own <form> inside the export form. Nested
    forms are invalid HTML, so browsers dropped the inner one and the ❗ button
    submitted the export form instead — the reported 405."""
    _ingest_with_authors(client, monkeypatch, crossref_message, "10.9999/nested", ["Adams"])
    page = client.get("/").text

    table = page[page.index("js-export-form"):page.index("</table>")]
    assert "<form" not in table, "no second <form> may open inside the export form"
    assert 'formaction="/articles/1/cite-first"' in table


def test_cite_first_marks_the_whole_row(client, monkeypatch, crossref_message) -> None:
    _ingest_with_authors(client, monkeypatch, crossref_message, "10.9999/rowmark", ["Adams"])
    assert 'class="cite-first-row"' not in client.get("/").text

    marked = client.post("/articles/1/cite-first", data={"sort": "year", "order": "desc"})
    assert 'class="cite-first-row"' in marked.text
    assert "cite-first-active" in marked.text

    unmarked = client.post("/articles/1/cite-first", data={"sort": "year", "order": "desc"})
    assert 'class="cite-first-row"' not in unmarked.text


def test_cite_first_glyph_is_not_the_red_emoji(client, monkeypatch, crossref_message) -> None:
    """❗ comes from a colour-emoji font and ignores CSS `color`, so it cannot be
    made green; the button has to use a plain text exclamation mark."""
    _ingest_with_authors(client, monkeypatch, crossref_message, "10.9999/glyph", ["Adams"])
    assert "❗" not in client.get("/").text


def test_cite_first_papers_are_pinned_to_the_top(client, monkeypatch, crossref_message) -> None:
    for i, year in enumerate((2026, 2020, 2015)):
        _ingest_with_dates(client, monkeypatch, crossref_message, f"10.9999/pin{i}", [year, 1])

    page = client.get("/").text
    assert page.index("Paper 2026-1") < page.index("Paper 2015-1")  # normal order

    # Flag the oldest paper; it must jump above the newest.
    from sqlalchemy import select

    from app import db
    from app.models import Article

    with db.SessionLocal() as session:
        oldest_id = session.scalar(select(Article.id).where(Article.year == 2015))
    client.post(f"/articles/{oldest_id}/cite-first", data={"sort": "year", "order": "desc"})

    page = client.get("/").text
    assert page.index("Paper 2015-1") < page.index("Paper 2026-1")


def test_pin_survives_every_sort_column(client, monkeypatch, crossref_message) -> None:
    for i, year in enumerate((2026, 2015)):
        _ingest_with_dates(client, monkeypatch, crossref_message, f"10.9999/ps{i}", [year, 1])
    from sqlalchemy import select

    from app import db
    from app.models import Article

    with db.SessionLocal() as session:
        oldest_id = session.scalar(select(Article.id).where(Article.year == 2015))
    client.post(f"/articles/{oldest_id}/cite-first", data={"sort": "year", "order": "desc"})

    # Including "author", which is sorted in Python after the SQL ORDER BY.
    for query in ("", "?sort=title", "?sort=year&order=asc", "?sort=author", "?sort=journal"):
        page = client.get(f"/{query}").text
        assert page.index("Paper 2015-1") < page.index("Paper 2026-1"), f"pin lost for {query!r}"


def test_two_foldable_tables_with_counts(client, monkeypatch, crossref_message) -> None:
    for i, year in enumerate((2026, 2020, 2015)):
        _ingest_with_dates(client, monkeypatch, crossref_message, f"10.9999/two{i}", [year, 1])

    # Nothing flagged yet: only the "All research" section exists.
    page = client.get("/").text
    assert "Research requested citing" not in page
    assert "All research" in page

    from sqlalchemy import select

    from app import db
    from app.models import Article

    with db.SessionLocal() as session:
        flagged_id = session.scalar(select(Article.id).where(Article.year == 2015))
    page = client.post(
        f"/articles/{flagged_id}/cite-first", data={"sort": "year", "order": "desc"}
    ).text

    assert "Research requested citing" in page
    assert page.index("Research requested citing") < page.index("All research")
    # Counts: one flagged, three overall.
    cited_head = page[page.index("Research requested citing"):page.index("All research")]
    assert '<span class="count-badge">1</span>' in cited_head
    all_head = page[page.index("All research"):]
    assert '<span class="count-badge">3</span>' in all_head
    # Both sections are <details>, so both fold.
    assert page.count("<details open>") == 2


def test_each_table_gets_its_own_export_form(client, monkeypatch, crossref_message) -> None:
    """Select-all and the counter must apply per table, not across both."""
    _ingest_with_dates(client, monkeypatch, crossref_message, "10.9999/forms", [2020, 1])
    from sqlalchemy import select

    from app import db
    from app.models import Article

    with db.SessionLocal() as session:
        flagged_id = session.scalar(select(Article.id))
    page = client.post(
        f"/articles/{flagged_id}/cite-first", data={"sort": "year", "order": "desc"}
    ).text

    assert page.count('class="js-export-form"') == 2
    # The flagged paper is listed in both tables, so it has two checkboxes.
    assert page.count(f'name="ids" value="{flagged_id}"') == 2


def test_author_page_highlights_cite_first_rows_without_a_second_table(
    client, monkeypatch, crossref_message
) -> None:
    _ingest_with_authors(client, monkeypatch, crossref_message, "10.9999/ah1", ["Adams"])
    _ingest_with_authors(client, monkeypatch, crossref_message, "10.9999/ah2", ["Adams"])

    page = client.get("/authors/1").text
    assert "cite-first-row" not in page
    assert page.count("<table") == 1

    client.post("/articles/1/cite-first", data={"sort": "year", "order": "desc"})

    page = client.get("/authors/1").text
    assert page.count('class="cite-first-row"') == 1  # only the flagged one
    assert page.count("<table") == 1  # still a single table
    assert "Research requested citing" not in page  # no extra section here
