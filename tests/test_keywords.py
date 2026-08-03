import fitz

from app.ingest import extract_keywords_from_pdf, split_keywords


def _make_pdf(text: str | None = None, metadata: dict | None = None) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    if text:
        page.insert_text((72, 72), text)
    if metadata:
        doc.set_metadata(metadata)
    return doc.tobytes()


def test_split_keywords_separators_and_cleanup() -> None:
    assert split_keywords("machine learning, zeolites; catalysis.") == [
        "machine learning",
        "zeolites",
        "catalysis",
    ]


def test_split_keywords_dedupes_case_insensitively() -> None:
    assert split_keywords("Zeolites; zeolites, ZEOLITES") == ["Zeolites"]


def test_split_keywords_skips_junk() -> None:
    assert split_keywords(";; , ." + ", " + "x" * 100) == []


def test_keywords_from_pdf_metadata() -> None:
    pdf = _make_pdf(text="Some body text", metadata={"keywords": "solar cells; perovskite"})
    assert extract_keywords_from_pdf(pdf) == ["solar cells", "perovskite"]


def test_keywords_from_pdf_text_line() -> None:
    pdf = _make_pdf(text="A Title\nAbstract blah blah.\nKeywords: alpha, beta; gamma delta.\nBody")
    assert extract_keywords_from_pdf(pdf) == ["alpha", "beta", "gamma delta"]


def test_no_keywords_anywhere() -> None:
    pdf = _make_pdf(text="Nothing to see here")
    assert extract_keywords_from_pdf(pdf) == []


def _article_with_stored_pdf(pdf_bytes: bytes, title: str) -> int:
    """Simulate a record ingested before keyword scrubbing existed."""
    from app import db, ingest
    from app.models import Article

    with db.SessionLocal() as session:
        article = Article(
            title=title, status="ready", pdf_path=ingest.save_pdf(pdf_bytes, None)
        )
        session.add(article)
        session.commit()
        return article.id


def test_rescan_stored_pdf_links_keywords(client) -> None:
    pdf = _make_pdf(text="Keywords: thermal hydraulics; fuel cladding")
    article_id = _article_with_stored_pdf(pdf, "Old Record")

    resp = client.post(f"/articles/{article_id}/rescan")
    assert resp.json()["keywords"] == ["thermal hydraulics", "fuel cladding"]

    detail = client.get(f"/api/articles/{article_id}").json()
    assert detail["keywords"] == ["thermal hydraulics", "fuel cladding"]

    # browser posts land back on the article page with a result banner
    page = client.post(
        f"/articles/{article_id}/rescan",
        headers={"accept": "text/html"},
        follow_redirects=True,
    )
    assert "PDF rescanned" in page.text


def test_bulk_rescan_covers_all_stored_pdfs(client) -> None:
    with_kw = _article_with_stored_pdf(
        _make_pdf(text="Keywords: corrosion"), "Has keywords"
    )
    without_kw = _article_with_stored_pdf(_make_pdf(text="Plain text"), "No keywords")

    body = client.post("/api/rescan-pdfs").json()
    assert body["pdfs_scanned"] == 2
    assert body["articles_with_keywords"] == {str(with_kw): ["corrosion"]}
    assert str(without_kw) not in body["articles_with_keywords"]


def test_uploaded_pdf_keywords_are_linked_and_shown_as_dashboard_chips(client) -> None:
    pdf = _make_pdf(text="Keywords: neutronics, reactor safety")
    resp = client.post(
        "/api/ingest",
        data={"title": "A DOI-less Report"},
        files={"file": ("report.pdf", pdf, "application/pdf")},
    )
    body = resp.json()
    assert body["status"] == "created"
    assert body["doi"] is None

    detail = client.get(f"/api/articles/{body['article_id']}").json()
    assert detail["keywords"] == ["neutronics", "reactor safety"]

    dashboard = client.get("/").text
    assert '<span class="chip">neutronics</span>' in dashboard
    assert '<span class="chip">reactor safety</span>' in dashboard


def test_keywords_set_vertically_one_per_line() -> None:
    # Regression: Elsevier prints the label on its own line with the keywords
    # stacked underneath, so the old same-line-only capture found nothing.
    pdf = _make_pdf(
        text=(
            "A Paper Title\n"
            "Keywords:\n"
            "MHD pumps\n"
            "Dual Fluid Reactor\n"
            "Optimization procedure\n"
            "a b s t r a c t\n"
            "The metallic version of the reactor uses a liquid eutectic."
        )
    )
    assert extract_keywords_from_pdf(pdf) == [
        "MHD pumps",
        "Dual Fluid Reactor",
        "Optimization procedure",
    ]


def test_vertical_keyword_list_stops_at_the_abstract() -> None:
    pdf = _make_pdf(
        text="Keywords:\nalpha\nbeta\nAbstract\ngamma should not be a keyword\n"
    )
    assert extract_keywords_from_pdf(pdf) == ["alpha", "beta"]


def test_inline_keywords_ignore_the_lines_below_them() -> None:
    # The vertical fallback must not fire when the label's own line has content,
    # or body text would leak in as keywords.
    pdf = _make_pdf(text="Keywords: alpha, beta\nBody text that is not a keyword\n")
    assert extract_keywords_from_pdf(pdf) == ["alpha", "beta"]


def test_split_keywords_treats_newlines_as_separators() -> None:
    assert split_keywords("alpha\nbeta, gamma") == ["alpha", "beta", "gamma"]
