"""Linking logged-in users to their author records (surname + first initial)."""

import pytest
from institute_auth import DirectoryUser

from app.people import fold, name_candidates, name_matches


def test_fold_strips_case_diacritics_and_polish_letters():
    assert fold("Michał  P.") == "michal p"
    assert fold("ŁUKASZ Żółć") == "lukasz zolc"
    assert fold("Kowalska-Nowak") == "kowalska-nowak"


@pytest.mark.parametrize(
    "byline",
    [
        "Michał Spirzewski",
        "Michal Spirzewski",  # PDF without diacritics
        "M. Spirzewski",
        "M Spirzewski",
        "Michał J. Spirzewski",  # middle initial
        "MICHAŁ SPIRZEWSKI",
        "Spirzewski, Michał",  # family-first with comma
    ],
)
def test_matches(byline):
    assert name_matches(byline, "Spirzewski", "Michał")


@pytest.mark.parametrize(
    "byline",
    [
        "Piotr Spirzewski",  # same surname, other initial
        "Michał Spirzewska",  # other surname
        "Spirzewski",  # no given name to check
        "Michał Spirzewski-Nowak",  # double-barrelled is a different surname
        "",
    ],
)
def test_does_not_match(byline):
    assert not name_matches(byline, "Spirzewski", "Michał")


def test_multi_word_surname():
    assert name_matches("Jan de Vries", "de Vries", "Jan")
    assert not name_matches("Jan Vries", "de Vries", "Jan")


def _user(**kwargs):
    base = dict(username="u", display_name="Spirzewski Michał", email=None,
                department=None, dn="CN=x")
    return DirectoryUser(**{**base, **kwargs})


def test_name_candidates_prefer_directory_split():
    assert name_candidates(_user(given_name="Michał", surname="Spirzewski")) == [
        ("Spirzewski", "Michał")
    ]
    # Older stored logins have no split: try the display name both ways.
    assert name_candidates(_user()) == [("Spirzewski", "Michał"), ("Michał", "Spirzewski")]
    assert name_candidates(_user(display_name="Mononym")) == []


# --------------------------------------------------------------------------- /me
def _author_with_paper(name: str, title: str, *, orcid: str | None = None,
                       merged_into: int | None = None) -> int:
    from app import db
    from app.models import Article, ArticleAuthor, Author

    with db.SessionLocal() as session:
        author = Author(full_name=name, orcid=orcid, merged_into_id=merged_into)
        article = Article(title=title, status="ready")
        session.add_all([author, article])
        session.flush()
        session.add(ArticleAuthor(article_id=article.id, author_id=author.id, position=0))
        session.commit()
        return author.id


def test_me_page_suggests_confirms_and_lists_papers(client):
    mine = _author_with_paper("Ania Tester", "Paper One", orcid="0000-0000-0000-0001")
    variant = _author_with_paper("A. Tester", "Paper Two")
    namesake = _author_with_paper("Bartek Tester", "Not Mine")

    page = client.get("/me").text
    assert f'value="{mine}"' in page and f'value="{variant}"' in page
    assert f'value="{namesake}"' not in page
    assert "Your papers" not in page  # nothing confirmed yet

    saved = client.post(
        "/me/authors", data={"author_id": [mine, variant]}, follow_redirects=False
    )
    assert saved.headers["location"] == "/me?saved=1"
    page = client.get("/me").text
    assert "Your papers (2)" in page
    assert "Paper One" in page and "Paper Two" in page and "Not Mine" not in page

    assert "you-badge" in client.get(f"/authors/{mine}").text
    assert "you-badge" not in client.get(f"/authors/{namesake}").text


def test_cannot_claim_an_author_that_does_not_match(client):
    namesake = _author_with_paper("Bartek Tester", "Not Mine")
    client.post("/me/authors", data={"author_id": [namesake]})
    assert "Your papers" not in client.get("/me").text


def test_unticking_removes_the_link(client):
    mine = _author_with_paper("Ania Tester", "Paper One")
    client.post("/me/authors", data={"author_id": [mine]})
    client.post("/me/authors", data={})
    assert f'value="{mine}" checked' not in client.get("/me").text


def test_merged_variant_suggests_its_canonical_author(client):
    canonical = _author_with_paper("Anna Kowalczyk-Tester", "Canonical Paper")
    _author_with_paper("Ania Tester", "Old Spelling", merged_into=canonical)
    page = client.get("/me").text
    assert f'value="{canonical}"' in page


def test_article_page_marks_you(client):
    from app import db
    from app.models import ArticleAuthor

    mine = _author_with_paper("Ania Tester", "Paper One")
    client.post("/me/authors", data={"author_id": [mine]})
    with db.SessionLocal() as session:
        from sqlalchemy import select

        article_id = session.scalar(
            select(ArticleAuthor.article_id).where(ArticleAuthor.author_id == mine)
        )
    assert "you-badge" in client.get(f"/articles/{article_id}").text


def test_me_needs_login(anon_client):
    response = anon_client.get("/me", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fme"


# --------------------------------------------------------------------------- notes
def _ingested(client) -> int:
    return client.post("/api/ingest", data={"doi": "10.9999/notes"}).json()["article_id"]


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("citation-examples", {"citation_examples": "x"}),
        ("highlights", {"highlights": "x"}),
        ("fetch-highlights", {}),
    ],
)
def test_logged_in_non_author_cannot_edit_notes(client, path, data):
    article_id = _ingested(client)
    response = client.post(f"/articles/{article_id}/{path}", data=data)
    assert response.status_code == 403


def test_non_author_sees_notes_read_only_with_hint(client):
    article_id = _ingested(client)
    page = client.get(f"/articles/{article_id}").text
    assert f'action="/articles/{article_id}/highlights"' not in page
    assert f'action="/articles/{article_id}/citation-examples"' not in page
    assert "fetch-highlights" not in page
    assert 'href="/me">My publications</a>' in page


def test_confirmed_author_can_edit_notes(client):
    from auth_helpers import claim_authorship

    article_id = _ingested(client)
    claim_authorship(article_id)
    page = client.get(f"/articles/{article_id}").text
    assert f'action="/articles/{article_id}/highlights"' in page
    assert "notes-hint" not in page
    response = client.post(f"/articles/{article_id}/highlights", data={"highlights": "Mine."})
    assert response.status_code == 200
    assert "Mine." in response.text


# --------------------------------------------------------------------------- department
def _roster_user(roster):
    from dataclasses import replace

    from auth_helpers import TEST_USER

    return replace(TEST_USER, department_roster=roster)


def _person(username, given, surname, active=True):
    from institute_auth import DirectoryPerson

    return DirectoryPerson(username, f"{surname} {given}", given, surname, "UZ3", active)


def test_login_stores_and_replaces_department_roster(client):
    from app import db
    from app.auth import DbUserStore
    from app.models import DepartmentMember

    store = DbUserStore()
    store.save(_roster_user((_person("jkowal", "Jan", "Kowalski"),
                             _person("enowak", "Ewa", "Nowak", active=False))))
    with db.SessionLocal() as session:
        rows = {m.username: m for m in session.query(DepartmentMember)}
    assert set(rows) == {"jkowal", "enowak"}
    assert rows["jkowal"].department == "UZ3" and not rows["enowak"].active

    store.save(_roster_user((_person("jkowal", "Jan", "Kowalski"),)))  # Ewa moved away
    with db.SessionLocal() as session:
        assert [m.username for m in session.query(DepartmentMember)] == ["jkowal"]

    store.save(_roster_user(None))  # roster unreadable this time: keep what we had
    with db.SessionLocal() as session:
        assert [m.username for m in session.query(DepartmentMember)] == ["jkowal"]


def test_department_authors_from_roster_and_confirmed_links(client):
    from app import db
    from app.auth import DbUserStore
    from app.people import department_authors

    colleague = _author_with_paper("J. Kowalski", "Colleague paper")
    outsider = _author_with_paper("Adam Kowalski", "Namesake surname, other initial")
    me = _author_with_paper("Ania Tester", "My paper")

    DbUserStore().save(_roster_user((_person("jkowal", "Jan", "Kowalski"),)))
    client.post("/me/authors", data={"author_id": [me]})

    with db.SessionLocal() as session:
        found = department_authors(session, "UZ3")
        assert found[colleague] == {"jkowal"}
        assert found[me] == {"tester"}  # confirmed by a UZ3 user, not in the roster
        assert outsider not in found
        assert department_authors(session, "OTHER") == {}
