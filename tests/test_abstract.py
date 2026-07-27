import textwrap

import fitz

from app.ingest import extract_abstract_from_pdf


def _make_pdf(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    return doc.tobytes()


_LONG_ABSTRACT = (
    "This study investigates the thermal-hydraulic behaviour of a novel fuel "
    "assembly design under transient conditions, showing a measurable "
    "reduction in peak cladding temperature compared to the reference case."
)
# insert_text() doesn't wrap long lines to the page width, so line breaks here
# stand in for the natural wrapping every real PDF paragraph has.
_WRAPPED_ABSTRACT = "\n".join(textwrap.wrap(_LONG_ABSTRACT, width=70))


def test_abstract_stops_before_keywords_heading() -> None:
    pdf = _make_pdf(
        f"A Title\nAbstract\n{_WRAPPED_ABSTRACT}\nKeywords: fuel; cladding\n1. Introduction\nBody"
    )
    assert extract_abstract_from_pdf(pdf) == _LONG_ABSTRACT


def test_abstract_stops_before_introduction_heading() -> None:
    pdf = _make_pdf(f"A Title\nAbstract\n{_WRAPPED_ABSTRACT}\n1. Introduction\nBody")
    assert extract_abstract_from_pdf(pdf) == _LONG_ABSTRACT


def test_abstract_handles_letter_spaced_heading() -> None:
    # Common in two-column Elsevier-style templates where the heading font
    # inserts wide letter spacing, which PDF text extraction preserves as gaps.
    pdf = _make_pdf(f"A Title\nA B S T R A C T\n{_WRAPPED_ABSTRACT}\nKeywords: fuel\nBody")
    assert extract_abstract_from_pdf(pdf) == _LONG_ABSTRACT


def test_no_abstract_heading_returns_none() -> None:
    pdf = _make_pdf("Just some body text with no heading at all.")
    assert extract_abstract_from_pdf(pdf) is None


def test_abstract_heading_with_no_real_body_returns_none() -> None:
    pdf = _make_pdf("Abstract\nKeywords: foo, bar")
    assert extract_abstract_from_pdf(pdf) is None


def test_uploaded_elsevier_style_pdf_backfills_missing_abstract(client) -> None:
    """Crossref/S2 abstracts are mocked empty in this fixture (mirrors Elsevier,
    which typically omits `abstract` from its Crossref records)."""
    pdf = _make_pdf(
        f"A Title\nAbstract\n{_WRAPPED_ABSTRACT}\nKeywords: fuel\n1. Introduction\nBody"
    )
    resp = client.post(
        "/api/ingest",
        data={"title": "An Elsevier-style Paper"},
        files={"file": ("paper.pdf", pdf, "application/pdf")},
    )
    body = resp.json()
    assert body["status"] == "created"

    detail = client.get(f"/api/articles/{body['article_id']}").json()
    from app import db
    from app.models import Article

    with db.SessionLocal() as session:
        article = session.get(Article, detail["id"])
        assert article.abstract == _LONG_ABSTRACT


def test_crossref_abstract_takes_priority_over_pdf_grep(
    client, monkeypatch, crossref_message
) -> None:
    from app import ingest

    monkeypatch.setattr(ingest, "fetch_crossref", lambda _doi: crossref_message)
    pdf = _make_pdf(
        f"A Title\nAbstract\n{_WRAPPED_ABSTRACT}\nKeywords: fuel\n1. Introduction\nBody"
    )
    resp = client.post(
        "/api/ingest",
        data={"doi": "10.1038/nphys1170"},
        files={"file": ("paper.pdf", pdf, "application/pdf")},
    )
    body = resp.json()

    from app import db
    from app.models import Article

    with db.SessionLocal() as session:
        article = session.get(Article, body["article_id"])
        # Crossref's real abstract (from the fixture) wins over the PDF-grepped one.
        assert article.abstract == "Quantum measurement & its 100% weird consequences."
