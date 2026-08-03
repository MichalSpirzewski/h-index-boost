"""The 'Download summary' export: a self-contained copy of the dashboard in a ZIP."""

import copy
import io
import re
import zipfile


def _ingest(client, monkeypatch, base_message, doi, title, family, with_pdf=False, **extra):
    from app import ingest

    message = copy.deepcopy(base_message)
    message["DOI"] = doi
    message["title"] = [title]
    message["author"] = [{"given": "Alex", "family": family, "sequence": "first"}]
    message.update(extra)
    monkeypatch.setattr(ingest, "fetch_crossref", lambda _doi, m=message: m)
    files = (
        {"file": (f"{family}.pdf", b"%PDF-1.4 offline export test", "application/pdf")}
        if with_pdf
        else None
    )
    body = client.post("/api/ingest", data={"doi": doi}, files=files).json()
    return body["article_id"]


def _archive(client, ids):
    response = client.post("/export/site", data={"ids": ids})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert f'filename="refbase-{len(ids)}-page.zip"' in response.headers["content-disposition"]
    return zipfile.ZipFile(io.BytesIO(response.content))


def test_every_export_bar_offers_the_page_download(client, monkeypatch, crossref_message) -> None:
    _ingest(client, monkeypatch, crossref_message, "10.5555/bar", "Bar Paper", "Adams")
    for url in ("/", "/authors/1", "/journals/Nature%20Physics", "/search?q=Bar"):
        page = client.get(url)
        assert page.status_code == 200
        assert 'formaction="/export/site"' in page.text, url
        assert 'formaction="/shares"' in page.text, url


def test_archive_contains_only_summary_and_flat_pdfs(
    client, monkeypatch, crossref_message
) -> None:
    first = _ingest(
        client, monkeypatch, crossref_message, "10.5555/one", "First Paper", "Adams",
        with_pdf=True,
    )
    second = _ingest(
        client, monkeypatch, crossref_message, "10.5555/two", "Second Paper", "Baker"
    )

    with _archive(client, [first, second]) as archive:
        names = archive.namelist()
        assert names == ["summary.html", "adams2009first.pdf"]
        # One PDF was uploaded; the paper without one contributes no file.
        page = archive.read("summary.html").decode()

    # CSS and JavaScript are embedded, so the page is the only supporting file.
    assert "<style>" in page
    assert "<script>" in page
    assert 'href="style.css"' not in page
    assert 'src="offline.js"' not in page
    assert "/static/" not in page
    # Inline assets are trusted project files and must be emitted verbatim.
    # HTML-escaping them leaves the page visible but makes its CSS selectors and
    # JavaScript invalid, breaking title expansion, filtering and sorting.
    style = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
    script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
    assert ".dashboard-panels-left > .authors-panel:last-child" in style
    assert 'document.querySelectorAll("th.sortable a")' in script
    assert "&#34;" not in script
    assert "&amp;&amp;" not in script
    assert not any(name.endswith(".bib") for name in names)
    assert not any(name.endswith(".xml") for name in names)
    # Only the paper that has one gets PDF actions, pointing beside summary.html.
    assert 'href="adams2009first.pdf"' in page
    assert "baker2009second.pdf" not in page


def test_page_contains_only_the_selected_papers(client, monkeypatch, crossref_message) -> None:
    keep = _ingest(client, monkeypatch, crossref_message, "10.5555/keep", "Kept Paper", "Adams")
    _ingest(client, monkeypatch, crossref_message, "10.5555/drop", "Dropped Paper", "Baker")

    with _archive(client, [keep]) as archive:
        page = archive.read("summary.html").decode()

    assert "Kept Paper" in page
    assert "Dropped Paper" not in page
    assert "Baker" not in page  # nor in the authors panel


def test_page_mirrors_the_dashboard_table_and_panels(
    client, monkeypatch, crossref_message
) -> None:
    article_id = _ingest(
        client, monkeypatch, crossref_message, "10.5555/mirror", "Mirror Paper", "Adams",
        with_pdf=True,
    )
    client.post(f"/articles/{article_id}/cite-first", data={"sort": "year", "order": "desc"})

    with _archive(client, [article_id]) as archive:
        page = archive.read("summary.html").decode()

    for column in (">Title<", ">Authors<", ">Published<", ">Online<", ">Journal<", ">Status<"):
        assert column in page
    assert ">Cite first<" in page and ">PDF<" in page
    # Panels and the flagged-papers section come across too.
    assert '<span class="panel-title">Authors</span>' in page
    assert '<span class="panel-title">Keywords</span>' in page
    assert "Research requested citing" in page
    assert 'class="cite-first-row"' in page
    assert "Download .bib" not in page
    assert "Download for Word" not in page
    # Sorting/filtering hooks the offline script needs.
    assert 'data-sort-key="published"' in page
    assert 'data-sort-published="2009-04"' in page  # the fixture's issue date
    assert 'class="js-author-filter"' in page


def test_expanded_summary_has_two_columns_and_foldable_bibtex(
    client, monkeypatch, crossref_message
) -> None:
    article_id = _ingest(
        client, monkeypatch, crossref_message, "10.5555/detail", "Detailed Paper", "Adams"
    )

    with _archive(client, [article_id]) as archive:
        page = archive.read("summary.html").decode()

    assert 'class="detail-metadata"' in page
    assert 'class="detail-content"' in page
    assert 'class="detail-bibtex-fold"' in page
    assert "<summary>BibTeX</summary>" in page
    assert "grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)" in page


def test_cite_keys_are_unique_across_the_whole_selection(
    client, monkeypatch, crossref_message
) -> None:
    """Two papers by the same author in the same year share a base cite key; the
    a/b suffixes have to reach the PDF filenames too, or one would overwrite the
    other in the archive."""
    ids = [
        _ingest(
            client, monkeypatch, crossref_message, f"10.5555/dup{n}", "Same Title", "Adams",
            with_pdf=True,
        )
        for n in (1, 2)
    ]

    with _archive(client, ids) as archive:
        pdfs = sorted(name for name in archive.namelist() if name.endswith(".pdf"))
        page = archive.read("summary.html").decode()

    assert pdfs == ["adams2009same.pdf", "adams2009samea.pdf"]
    for name in pdfs:
        assert f'href="{name}"' in page


def test_page_export_with_nothing_selected_is_a_clean_400(client) -> None:
    assert client.post("/export/site", data={}).status_code == 400
