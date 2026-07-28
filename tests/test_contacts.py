"""Auxiliary NCBJ contact data: file storage + the add-contact route."""

import copy

import pytest

from app import contacts


@pytest.fixture(autouse=True)
def _clean_contacts_file():
    """The contacts JSON lives in the session-wide temp data dir; reset it per test."""
    path = contacts._path()
    if path.exists():
        path.unlink()
    yield
    if path.exists():
        path.unlink()


def _ingest_ncbj_author(client, monkeypatch, crossref_message):
    """Ingest one article whose sole author is affiliated with NCBJ, return author id."""
    from app import ingest

    message = copy.deepcopy(crossref_message)
    message["DOI"] = "10.1234/ncbj-author"
    message["title"] = ["An NCBJ Paper"]
    message["author"] = [
        {
            "given": "Anna",
            "family": "Nowak",
            "sequence": "first",
            "affiliation": [{"name": "National Centre for Nuclear Research, Otwock, Poland"}],
        }
    ]
    monkeypatch.setattr(ingest, "fetch_crossref", lambda _doi: message)
    client.post("/api/ingest", data={"doi": "10.1234/ncbj-author"})

    from sqlalchemy import select

    from app import db
    from app.models import Author

    with db.SessionLocal() as session:
        return session.scalar(select(Author.id).where(Author.full_name == "Anna Nowak"))


def test_save_and_get_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(contacts.db, "DATA_DIR", tmp_path)
    contacts.save(
        7, "Jane Doe", {"phone": "+48 1", "meeting_link": ""}
    )
    stored = contacts.get(7)
    assert stored["phone"] == "+48 1"
    assert "meeting_link" not in stored  # blank field not stored
    assert stored["name"] == "Jane Doe"


def test_clearing_all_fields_removes_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(contacts.db, "DATA_DIR", tmp_path)
    contacts.save(7, "Jane Doe", {"phone": "+48 1"})
    assert contacts.get(7)
    contacts.save(7, "Jane Doe", {"phone": "", "meeting_link": ""})
    assert contacts.get(7) == {}


def test_ncbj_author_page_has_contact_form(client, monkeypatch, crossref_message):
    author_id = _ingest_ncbj_author(client, monkeypatch, crossref_message)
    page = client.get(f"/authors/{author_id}").text
    assert "NCBJ contact" in page
    assert f'action="/authors/{author_id}/contact"' in page


def test_post_contact_persists_and_renders(client, monkeypatch, crossref_message):
    author_id = _ingest_ncbj_author(client, monkeypatch, crossref_message)
    resp = client.post(
        f"/authors/{author_id}/contact",
        data={"phone": "+48 22 273 13 04", "meeting_link": "https://meet.example/xyz"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert contacts.get(author_id)["phone"] == "+48 22 273 13 04"

    page = client.get(f"/authors/{author_id}").text
    assert "https://meet.example/xyz" in page


def test_non_ncbj_author_has_no_contact_card(client, monkeypatch, crossref_message):
    # The default crossref_sample authors carry no NCBJ affiliation.
    client.post("/api/ingest", data={"doi": "10.5555/other"})
    from sqlalchemy import select

    from app import db
    from app.models import Author

    with db.SessionLocal() as session:
        author_id = session.scalar(select(Author.id))
    if author_id is not None:
        assert "NCBJ contact" not in client.get(f"/authors/{author_id}").text
