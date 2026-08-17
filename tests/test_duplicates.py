"""Duplicate guards and soft delete.

A paper with no DOI has nothing unique to key on, which is how a library ends up
with three copies of one upload. The file's own hash is the guard; hiding is the
way to clear the copies already there.
"""

import fitz
from sqlalchemy import func, select

from app import db, ingest
from app.models import Article


def _make_pdf(text: str) -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), text)
    return doc.tobytes()


def _visible_count() -> int:
    with db.SessionLocal() as session:
        return session.scalar(
            select(func.count(Article.id)).where(Article.hidden.is_(False))
        )


def _upload(client, pdf_bytes: bytes, **data):
    return client.post(
        "/api/ingest",
        data=data,
        files={"file": ("paper.pdf", pdf_bytes, "application/pdf")},
    )


def test_the_same_file_uploaded_twice_lands_on_the_first_record(client) -> None:
    pdf = _make_pdf("A DOI-less report with no identifiers at all.")
    first = _upload(client, pdf, title="A DOI-less Report").json()
    assert first["status"] == "created"

    second = _upload(client, pdf, title="A DOI-less Report").json()
    assert second["status"] == "already_exists"
    assert second["article_id"] == first["article_id"]
    assert second["url"] == f"/articles/{first['article_id']}"
    assert _visible_count() == 1


def test_a_third_upload_by_someone_else_is_also_caught(client) -> None:
    # The accident this guards: the same PDF added by two people and again by one
    # of them, each time with different "your name" and title text.
    pdf = _make_pdf("Computer codes in the safety analysis for nuclear power plants.")
    first = _upload(client, pdf, added_by="E.Skrzypek").json()
    _upload(client, pdf, added_by="E.Skrzypek, M.Skrzypek", title="Computer codes")
    third = _upload(client, pdf, added_by="M.Skrzypek", url="https://papers.example/592")

    assert third.json()["article_id"] == first["article_id"]
    assert _visible_count() == 1


def test_a_different_file_is_still_a_new_record(client) -> None:
    _upload(client, _make_pdf("First paper."), title="First")
    second = _upload(client, _make_pdf("A different paper."), title="Second").json()
    assert second["status"] == "created"
    assert _visible_count() == 2


def test_a_browser_upload_of_a_known_file_is_redirected_to_the_original(client) -> None:
    pdf = _make_pdf("A DOI-less report.")
    created = _upload(client, pdf, title="A DOI-less Report").json()

    page = client.post(
        "/api/ingest",
        data={"title": "A DOI-less Report"},
        files={"file": ("paper.pdf", pdf, "application/pdf")},
        headers={"accept": "text/html"},
        follow_redirects=True,
    )
    assert "already in the library" in page.text
    assert f"/articles/{created['article_id']}/bibtex" in page.text
    assert _visible_count() == 1


def test_the_doi_path_is_untouched_by_the_hash_guard(client) -> None:
    """A DOI resolves first, so the same file can still enrich a DOI'd record."""
    pdf = _make_pdf("A paper whose DOI the uploader knows.")
    created = _upload(client, pdf, doi="10.1038/nphys1170").json()
    assert created["status"] == "created"
    assert created["doi"] == "10.1038/nphys1170"

    again = _upload(client, pdf, doi="10.1038/nphys1170").json()
    assert again["status"] == "already_exists"
    assert again["article_id"] == created["article_id"]


def test_re_adding_a_paper_somebody_hid_is_allowed(client) -> None:
    """A hidden record must not block a re-upload — its link would 404."""
    pdf = _make_pdf("A DOI-less report.")
    created = _upload(client, pdf, title="A DOI-less Report").json()
    client.post(f"/articles/{created['article_id']}/hide")

    again = _upload(client, pdf, title="A DOI-less Report").json()
    assert again["status"] == "created"
    assert again["article_id"] != created["article_id"]


def test_hashes_are_backfilled_for_pdfs_stored_before_the_column(client) -> None:
    pdf = _make_pdf("A record from before the guard existed.")
    with db.SessionLocal() as session:
        article = Article(title="Legacy", status="ready", pdf_path=ingest.save_pdf(pdf, None))
        session.add(article)
        session.commit()
        article_id = article.id

    with db.SessionLocal() as session:
        assert ingest.backfill_pdf_hashes(session) == 1
        assert session.get(Article, article_id).pdf_sha256 == ingest.pdf_sha256(pdf)
        # Idempotent: a second pass has nothing left to do.
        assert ingest.backfill_pdf_hashes(session) == 0

    # And the backfilled record now guards against a re-upload.
    assert _upload(client, pdf, title="Legacy").json()["article_id"] == article_id


# --------------------------------------------------------------------------- soft delete


def _two_records(client) -> tuple[int, int]:
    first = _upload(client, _make_pdf("Keeper paper."), title="Keeper").json()
    second = _upload(client, _make_pdf("Copy to remove."), title="Accidental Copy").json()
    return first["article_id"], second["article_id"]


def test_hiding_removes_a_record_from_every_listing(client) -> None:
    keeper, copy_id = _two_records(client)

    assert client.post(f"/articles/{copy_id}/hide").json() == {
        "article_id": copy_id,
        "hidden": True,
    }

    dashboard = client.get("/").text
    assert "Keeper" in dashboard
    assert "Accidental Copy" not in dashboard
    assert "Accidental Copy" not in client.get("/search?q=Copy").text
    assert client.get(f"/articles/{copy_id}").status_code == 404
    assert client.get(f"/articles/{keeper}").status_code == 200


def test_hidden_page_lists_and_restores(client) -> None:
    _keeper, copy_id = _two_records(client)
    client.post(f"/articles/{copy_id}/hide")

    listing = client.get("/hidden").text
    assert "Accidental Copy" in listing
    assert f"/articles/{copy_id}/unhide" in listing
    # The dashboard advertises the hidden list only while something is in it.
    assert 'href="/hidden"' in client.get("/").text

    client.post(f"/articles/{copy_id}/unhide")
    assert client.get(f"/articles/{copy_id}").status_code == 200
    assert "Accidental Copy" in client.get("/").text
    assert "Nothing is hidden" in client.get("/hidden").text
    assert 'href="/hidden"' not in client.get("/").text


def test_browser_hide_lands_on_the_dashboard_with_a_restore_hint(client) -> None:
    _keeper, copy_id = _two_records(client)
    page = client.post(
        f"/articles/{copy_id}/hide",
        headers={"accept": "text/html"},
        follow_redirects=True,
    )
    assert "not deleted" in page.text
    assert "restore it from the hidden list" in page.text


def test_the_article_page_offers_hiding_with_a_confirmation(client) -> None:
    keeper, _copy = _two_records(client)
    page = client.get(f"/articles/{keeper}").text
    assert f'action="/articles/{keeper}/hide"' in page
    assert "onsubmit=\"return confirm(" in page


def test_hiding_twice_or_an_unknown_record_is_404(client) -> None:
    _keeper, copy_id = _two_records(client)
    assert client.post(f"/articles/{copy_id}/hide").status_code == 200
    assert client.post(f"/articles/{copy_id}/hide").status_code == 404
    assert client.post("/articles/999999/hide").status_code == 404
    assert client.post("/articles/999999/unhide").status_code == 404
