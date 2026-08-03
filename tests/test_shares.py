"""Persistent RefBase links for selected publications."""

import re
from urllib.parse import urlsplit

from sqlalchemy import select


def _articles():
    from app import db
    from app.models import Article, ArticleAuthor, Author, Keyword

    with db.SessionLocal() as session:
        keyword = Keyword(name="Shared keyword")
        papers = []
        for index, title in enumerate(("First shared paper", "Second shared paper"), start=1):
            author = Author(full_name=f"Alex Author{index}")
            paper = Article(
                title=title,
                year=2020 + index,
                abstract=f"Abstract for paper {index}.",
                journal="Sharing Journal",
                status="ready",
            )
            paper.author_links = [
                ArticleAuthor(
                    position=0,
                    author=author,
                    affiliation="National Centre for Nuclear Research",
                )
            ]
            paper.keywords = [keyword]
            session.add(paper)
            papers.append(paper)
        unselected = Article(title="Not selected", status="ready")
        session.add(unselected)
        session.commit()
        return [paper.id for paper in papers], unselected.id


def test_create_share_returns_persistent_opaque_url_with_selected_order(client) -> None:
    from app import db
    from app.models import SharedSelection, SharedSelectionArticle

    ids, _unselected_id = _articles()
    response = client.post("/shares", data={"ids": [ids[1], ids[0], ids[1]]})

    assert response.status_code == 201
    payload = response.json()
    assert payload["article_count"] == 2
    path = urlsplit(payload["url"]).path
    assert re.fullmatch(r"/shares/[A-Za-z0-9_-]{32}", path)

    with db.SessionLocal() as session:
        selection = session.get(SharedSelection, payload["token"])
        assert selection is not None
        links = list(
            session.scalars(
                select(SharedSelectionArticle)
                .where(SharedSelectionArticle.selection_token == payload["token"])
                .order_by(SharedSelectionArticle.position)
            )
        )
        assert [link.article_id for link in links] == [ids[1], ids[0]]


def test_shared_page_shows_only_selected_papers_with_summary_features(client) -> None:
    ids, _unselected_id = _articles()
    created = client.post("/shares", data={"ids": ids}).json()
    path = urlsplit(created["url"]).path

    response = client.get(path)
    assert response.status_code == 200
    page = response.text
    assert "Shared selection" in page
    assert "First shared paper" in page
    assert "Second shared paper" in page
    assert "Not selected" not in page
    assert page.index("First shared paper") < page.index("Second shared paper")
    assert page.count(f'data-id="{ids[0]}"') == 1
    assert page.count(f'data-id="{ids[1]}"') == 1
    assert 'class="js-detail-toggle"' in page
    assert 'class="detail-metadata"' in page
    assert 'class="detail-content"' in page
    assert "<summary>BibTeX</summary>" in page
    assert 'id="js-copy-share"' in page
    assert f'value="{created["url"]}"' in page
    assert 'class="js-export-form"' in page
    assert page.count('class="js-row-check"') == 2
    for action in (
        "/export/bibtex",
        "/export/word-xml",
        "/export/pdfs",
        "/export/all",
        "/export/site",
        "/shares",
    ):
        assert f'formaction="{action}"' in page
    assert "Create share link" in page
    assert "Copy BibTeX" in page
    assert "summary.html" not in page
    assert "In this folder" not in page


def test_browser_share_creation_redirects_to_copyable_page(client) -> None:
    ids, _unselected_id = _articles()
    response = client.post(
        "/shares",
        data={"ids": ids},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert re.search(r"/shares/[A-Za-z0-9_-]{32}\?created=1$", response.headers["location"])
    page = client.get(response.headers["location"]).text
    assert "Share link created." in page


def test_shared_page_omits_papers_hidden_after_link_creation(client) -> None:
    from app import db
    from app.models import Article

    ids, _unselected_id = _articles()
    path = urlsplit(client.post("/shares", data={"ids": ids}).json()["url"]).path

    with db.SessionLocal() as session:
        session.get(Article, ids[1]).hidden = True
        session.commit()

    response = client.get(path)
    assert response.status_code == 200
    assert "First shared paper" in response.text
    assert "Second shared paper" not in response.text


def test_share_errors_are_clear(client) -> None:
    assert client.post("/shares", data={}).status_code == 400
    assert client.post("/shares", data={"ids": [99999]}).status_code == 404
    assert client.get("/shares/not-a-real-token").status_code == 404
