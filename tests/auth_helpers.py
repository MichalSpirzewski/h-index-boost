"""The fake directory used by the tests: who "logs in", and with which groups."""

from institute_auth import DirectoryUser, Group

TEST_GROUP_DN = "CN=Pracownicy - UZ3,OU=Pracownicy,OU=Distribution Groups,DC=ncbj,DC=gov,DC=pl"
OTHER_GROUP_DN = "CN=Projekt HTGR,OU=Transfer,DC=ncbj,DC=gov,DC=pl"

TEST_USER = DirectoryUser(
    username="tester",
    display_name="Tester Ania",
    email="tester@example.org",
    department="UZ3",
    dn="CN=Tester Ania,OU=Users,DC=ncbj,DC=gov,DC=pl",
    groups=(Group(TEST_GROUP_DN, "Pracownicy - UZ3"),),
    given_name="Ania",
    surname="Tester",
)
TEST_PASSWORD = "correct horse"


def fake_directory(username: str, password: str) -> DirectoryUser | None:
    return TEST_USER if (username, password) == (TEST_USER.username, TEST_PASSWORD) else None


def log_in(client) -> None:
    response = client.post(
        "/login",
        data={"username": TEST_USER.username, "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def claim_authorship(article_id: int) -> None:
    """Make the logged-in test user a confirmed author of the article (its first author)."""
    from app import db, ingest
    from app.models import ArticleAuthor, UserAuthor

    with db.SessionLocal() as session:
        link = session.query(ArticleAuthor).filter_by(article_id=article_id).first()
        assert link is not None, "article has no authors"
        canonical = ingest.canonical_author(session, link.author)
        session.merge(UserAuthor(username=TEST_USER.username, author_id=canonical.id))
        session.commit()
