"""Replacing an article's PDF and re-parsing the metadata that file supplies."""

import textwrap
from pathlib import Path

import fitz
from sqlalchemy import select

from app import db
from app.models import Article, ArticleAuthor


def _make_pdf(text: str, metadata: dict | None = None) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    if metadata:
        doc.set_metadata(metadata)
    return doc.tobytes()


def _wrapped(paragraph: str) -> str:
    """insert_text() doesn't wrap, so stand in for a real paragraph's line breaks."""
    return "\n".join(textwrap.wrap(paragraph, width=70))


_OLD_ABSTRACT = (
    "The first version of this manuscript reports a preliminary sweep of the "
    "coolant flow rate and reaches no firm conclusion about peak cladding "
    "temperature under the transient considered."
)
_NEW_ABSTRACT = (
    "The revised manuscript reports a full sweep of the coolant flow rate and "
    "demonstrates a measurable reduction in peak cladding temperature under the "
    "transient considered."
)


def _upload(client, article_id: int, pdf_bytes: bytes, filename: str = "new.pdf"):
    return client.post(
        f"/articles/{article_id}/replace-pdf",
        files={"file": (filename, pdf_bytes, "application/pdf")},
    )


def _stub_with_pdf(client, pdf_bytes: bytes, title: str = "A DOI-less Report") -> int:
    """A DOI-less record ingested with a PDF (no Crossref metadata involved)."""
    body = client.post(
        "/api/ingest",
        data={"title": title},
        files={"file": ("first.pdf", pdf_bytes, "application/pdf")},
    ).json()
    return body["article_id"]


def _article(article_id: int) -> Article:
    with db.SessionLocal() as session:
        return session.get(Article, article_id)


def test_replacement_stores_the_new_file_and_reparses_text_and_keywords(client) -> None:
    article_id = _stub_with_pdf(
        client, _make_pdf("Keywords: corrosion\nOnly the original body text.")
    )
    original_path = _article(article_id).pdf_path

    new_pdf = _make_pdf("Keywords: neutronics, reactor safety\nRevised body about xenon.")
    body = _upload(client, article_id, new_pdf).json()
    assert body["keywords"] == ["neutronics", "reactor safety"]

    article = _article(article_id)
    assert Path(article.pdf_path).read_bytes() == new_pdf
    assert not Path(original_path).exists()  # superseded file is not left behind
    assert "xenon" in article.pdf_text
    assert "original body text" not in article.pdf_text

    # The keywords the old file contributed are kept — they may be in a topic.
    detail = client.get(f"/api/articles/{article_id}").json()
    assert detail["keywords"] == ["corrosion", "neutronics", "reactor safety"]
    assert client.get("/search?q=xenon").text.count(f"/articles/{article_id}") > 0


def test_replacement_refreshes_an_abstract_that_came_from_the_old_pdf(client) -> None:
    article_id = _stub_with_pdf(
        client, _make_pdf(f"A Title\nAbstract\n{_wrapped(_OLD_ABSTRACT)}\n1. Introduction\n")
    )
    assert _article(article_id).abstract == _OLD_ABSTRACT

    body = _upload(
        client,
        article_id,
        _make_pdf(f"A Title\nAbstract\n{_wrapped(_NEW_ABSTRACT)}\n1. Introduction\n"),
    ).json()
    assert body["abstract_updated"] is True
    assert _article(article_id).abstract == _NEW_ABSTRACT


def test_replacement_keeps_an_abstract_that_came_from_crossref(client) -> None:
    created = client.post(
        "/api/ingest",
        data={"doi": "10.1038/nphys1170"},
        files={"file": ("first.pdf", _make_pdf("Body text only."), "application/pdf")},
    ).json()
    article_id = created["article_id"]
    crossref_abstract = _article(article_id).abstract
    assert crossref_abstract  # from the mocked Crossref message

    body = _upload(
        client,
        article_id,
        _make_pdf(f"A Title\nAbstract\n{_wrapped(_NEW_ABSTRACT)}\n1. Introduction\n"),
    ).json()
    assert body["abstract_updated"] is False
    assert _article(article_id).abstract == crossref_abstract


def _affiliations(article_id: int) -> list[str | None]:
    with db.SessionLocal() as session:
        links = session.scalars(
            select(ArticleAuthor)
            .where(ArticleAuthor.article_id == article_id)
            .order_by(ArticleAuthor.position)
        ).all()
        return [link.affiliation for link in links]


def test_replacement_reparses_author_affiliations_from_the_new_header(client) -> None:
    header = (
        "A Paper Title\nAlice B. Smith, Carol Danvers\n"
        "{affiliation}\nAbstract\nBody."
    )
    created = client.post(
        "/api/ingest",
        data={"doi": "10.1038/nphys1170"},
        files={
            "file": (
                "first.pdf",
                _make_pdf(
                    header.format(
                        affiliation="National Centre for Nuclear Research, Otwock, Poland"
                    )
                ),
                "application/pdf",
            )
        },
    ).json()
    article_id = created["article_id"]
    assert _affiliations(article_id) == [
        "National Centre for Nuclear Research, Otwock, Poland"
    ] * 2

    body = _upload(
        client,
        article_id,
        _make_pdf(header.format(affiliation="Poznan University of Technology, Poznan, Poland")),
    ).json()
    assert body["affiliations_filled"] == 2
    assert _affiliations(article_id) == [
        "Poznan University of Technology, Poznan, Poland"
    ] * 2


def test_doi_less_record_adopts_a_doi_printed_in_the_new_pdf(client) -> None:
    article_id = _stub_with_pdf(client, _make_pdf("A scan with no identifiers."))
    stub_path = _article(article_id).pdf_path

    body = _upload(
        client, article_id, _make_pdf("A Title\nhttps://doi.org/10.1038/NPhys1170\nBody.")
    ).json()
    assert body["doi_adopted"] == "10.1038/nphys1170"

    article = _article(article_id)
    assert article.doi == "10.1038/nphys1170"
    assert article.title == "Measured measurement"  # mocked Crossref ran after the upload
    assert article.status == "ready"
    # The file moved to its DOI-derived name; the UUID one is not left behind.
    assert article.pdf_path.endswith("10.1038_nphys1170.pdf")
    assert not Path(stub_path).exists()


def test_a_doi_another_record_already_holds_is_reported_not_stolen(client) -> None:
    owner = client.post("/api/ingest", data={"doi": "10.1038/nphys1170"}).json()
    article_id = _stub_with_pdf(client, _make_pdf("A scan with no identifiers."))

    body = _upload(
        client, article_id, _make_pdf("A Title\nhttps://doi.org/10.1038/nphys1170\nBody.")
    ).json()
    assert body["doi_adopted"] is None
    assert body["doi_conflict_article_id"] == owner["article_id"]
    assert _article(article_id).doi is None


def test_a_record_with_no_pdf_can_have_one_attached(client) -> None:
    article_id = client.post("/api/ingest", data={"title": "Metadata only"}).json()[
        "article_id"
    ]
    assert _article(article_id).pdf_path is None

    _upload(client, article_id, _make_pdf("Keywords: fuel cladding\nBody."))
    assert _article(article_id).pdf_path is not None
    assert client.get(f"/api/articles/{article_id}").json()["keywords"] == ["fuel cladding"]


def test_a_file_that_is_not_a_pdf_is_rejected_and_the_stored_one_survives(client) -> None:
    original = _make_pdf("Keywords: corrosion\nThe good file.")
    article_id = _stub_with_pdf(client, original)

    resp = _upload(client, article_id, b"PK\x03\x04 not a pdf at all", filename="notes.docx")
    assert resp.status_code == 422
    assert "not a PDF" in resp.json()["detail"]
    assert Path(_article(article_id).pdf_path).read_bytes() == original

    empty = client.post(
        f"/articles/{article_id}/replace-pdf",
        files={"file": ("empty.pdf", b"", "application/pdf")},
    )
    assert empty.status_code == 422
    assert Path(_article(article_id).pdf_path).read_bytes() == original


def test_browser_post_lands_back_on_the_article_page_with_a_banner(client) -> None:
    article_id = _stub_with_pdf(client, _make_pdf("The original."))

    page = client.post(
        f"/articles/{article_id}/replace-pdf",
        files={"file": ("new.pdf", _make_pdf("Keywords: alpha, beta"), "application/pdf")},
        headers={"accept": "text/html"},
        follow_redirects=True,
    )
    assert "PDF stored and re-parsed" in page.text
    assert "2 keywords found" in page.text

    rejected = client.post(
        f"/articles/{article_id}/replace-pdf",
        files={"file": ("notes.txt", b"just text", "text/plain")},
        headers={"accept": "text/html"},
        follow_redirects=True,
    )
    assert "That file is not a PDF" in rejected.text


def test_replace_pdf_on_a_missing_article_is_404(client) -> None:
    assert _upload(client, 999_999, _make_pdf("Body.")).status_code == 404
