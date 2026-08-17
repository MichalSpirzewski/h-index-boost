"""Topics — the curated layer that groups author keywords into browsable subjects."""

from sqlalchemy import select


def _library(*papers: tuple[str, list[str]]) -> tuple[dict[str, int], dict[str, int]]:
    """Create papers as (title, [keyword names]).

    Returns (article ids by title, keyword ids by name).
    """
    from app import db
    from app.models import Article, Keyword

    with db.SessionLocal() as session:
        keywords: dict[str, Keyword] = {}
        articles: dict[str, Article] = {}
        for title, names in papers:
            article = Article(title=title, year=2021, status="ready")
            for name in names:
                keyword = keywords.get(name)
                if keyword is None:
                    keyword = Keyword(name=name)
                    keywords[name] = keyword
                article.keywords.append(keyword)
            session.add(article)
            articles[title] = article
        session.commit()
        return (
            {title: article.id for title, article in articles.items()},
            {name: keyword.id for name, keyword in keywords.items()},
        )


def _create_topic(client, name: str, description: str | None = None) -> int:
    from app import db
    from app.models import Topic

    data = {"name": name}
    if description is not None:
        data["description"] = description
    response = client.post("/topics", data=data)
    assert response.status_code == 200  # 303 followed to the new topic page
    with db.SessionLocal() as session:
        return session.scalar(select(Topic.id).where(Topic.name == name))


def test_topic_page_offers_every_keyword_in_the_library(client) -> None:
    _library(("Paper A", ["neutronics"]), ("Paper B", ["corrosion", "welding"]))
    topic_id = _create_topic(client, "Reactor physics")

    page = client.get(f"/topics/{topic_id}").text
    # Classifying is a single click per keyword, so all of them are on the page.
    for keyword in ("neutronics", "corrosion", "welding"):
        assert keyword in page
    assert 'name="keyword_id"' in page
    assert f'action="/topics/{topic_id}/keywords"' in page


def test_clicking_a_keyword_classifies_it_and_clicking_it_again_removes_it(client) -> None:
    _articles, keywords = _library(("Paper A", ["neutronics"]))
    topic_id = _create_topic(client, "Reactor physics")

    added = client.post(
        f"/topics/{topic_id}/keywords", data={"keyword_id": keywords["neutronics"]}
    ).json()
    assert added["assigned"] is True

    page = client.get(f"/topics/{topic_id}").text
    assert "Keywords in this topic" in page
    assert "Paper A" in page  # the paper joined the topic through its keyword

    removed = client.post(
        f"/topics/{topic_id}/keywords", data={"keyword_id": keywords["neutronics"]}
    ).json()
    assert removed["assigned"] is False
    assert "Click keywords below to classify them" in client.get(f"/topics/{topic_id}").text


def test_topic_gathers_papers_from_all_its_keywords_counting_each_once(client) -> None:
    _articles, keywords = _library(
        ("Coolant Study", ["thermal hydraulics", "two-phase flow"]),
        ("Cladding Study", ["fuel cladding"]),
        ("Unrelated Study", ["cosmology"]),
    )
    topic_id = _create_topic(client, "Reactor engineering")
    for name in ("thermal hydraulics", "two-phase flow", "fuel cladding"):
        client.post(f"/topics/{topic_id}/keywords", data={"keyword_id": keywords[name]})

    # Scoped past the dashboard panels: Recent additions lists the whole library,
    # filter or no filter, so only the table below is expected to narrow.
    filtered = client.get(f"/?topic={topic_id}").text.split("<h1", 1)[-1]
    assert "Coolant Study" in filtered
    assert "Cladding Study" in filtered
    assert "Unrelated Study" not in filtered

    # The dashboard chip counts papers, not keyword hits: Coolant Study carries two
    # of this topic's keywords but must still count once.
    dashboard = client.get("/").text
    chip = dashboard[dashboard.index("Reactor engineering"):]
    assert '<span class="chip-count">2</span>' in chip[: chip.index("</a>")]


def test_dashboard_names_the_keyword_panel_keywords_and_lists_topics_apart(client) -> None:
    _articles, keywords = _library(("Paper A", ["neutronics"]))
    topic_id = _create_topic(client, "Reactor physics")
    client.post(f"/topics/{topic_id}/keywords", data={"keyword_id": keywords["neutronics"]})

    page = client.get("/").text
    assert '<span class="panel-title">Keywords</span>' in page
    assert '<span class="panel-title">Topics</span>' in page
    assert "Keywords &amp; topics" not in page  # the old combined label is gone
    assert f'href="/?topic={topic_id}"' in page
    assert f'href="/?keyword={keywords["neutronics"]}"' in page


def test_topic_names_stay_unique(client) -> None:
    _create_topic(client, "Reactor physics")
    clash = client.post("/topics", data={"name": "Reactor physics"})
    assert clash.status_code == 409
    assert "already exists" in clash.text


def test_topic_needs_a_name(client) -> None:
    blank = client.post("/topics", data={"name": "   "})
    assert blank.status_code == 422
    assert "A topic needs a name." in blank.text


def test_topic_can_be_renamed_and_described(client) -> None:
    topic_id = _create_topic(client, "Reactors")

    saved = client.post(
        f"/topics/{topic_id}",
        data={"name": "Reactor physics", "description": "Core behaviour and neutronics."},
    )
    assert saved.status_code == 200
    page = client.get(f"/topics/{topic_id}").text
    assert "Reactor physics" in page
    assert "Core behaviour and neutronics." in page


def test_deleting_a_topic_keeps_its_keywords_and_papers(client) -> None:
    _articles, keywords = _library(("Paper A", ["neutronics"]))
    topic_id = _create_topic(client, "Reactor physics")
    client.post(f"/topics/{topic_id}/keywords", data={"keyword_id": keywords["neutronics"]})

    assert client.post(f"/topics/{topic_id}/delete").json() == {"deleted": topic_id}
    assert client.get(f"/topics/{topic_id}").status_code == 404

    dashboard = client.get("/").text
    assert "Paper A" in dashboard
    assert "neutronics" in dashboard  # the keyword itself is untouched


def test_papers_report_the_topics_their_keywords_belong_to(client) -> None:
    articles, keywords = _library(("Paper A", ["neutronics"]))
    topic_id = _create_topic(client, "Reactor physics")
    client.post(f"/topics/{topic_id}/keywords", data={"keyword_id": keywords["neutronics"]})

    detail = client.get(f"/api/articles/{articles['Paper A']}").json()
    assert detail["keywords"] == ["neutronics"]
    assert detail["topics"] == ["Reactor physics"]

    page = client.get(f"/articles/{articles['Paper A']}").text
    assert f'href="/topics/{topic_id}"' in page


def test_unclassified_keywords_are_listed_on_the_topic_overview(client) -> None:
    _articles, keywords = _library(("Paper A", ["neutronics", "corrosion"]))
    topic_id = _create_topic(client, "Reactor physics")
    client.post(f"/topics/{topic_id}/keywords", data={"keyword_id": keywords["neutronics"]})

    page = client.get("/topics").text
    unclassified = page[page.index("Unclassified keywords"):]
    assert "corrosion" in unclassified
    assert "neutronics" not in unclassified


def _legacy_database(path, *, with_new_tables: bool = False):
    """A database as written before topics meant "group of keywords"."""
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE topics (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL UNIQUE)")
        )
        conn.execute(
            text(
                "CREATE TABLE article_topics ("
                "article_id INTEGER NOT NULL, "
                "topic_id INTEGER NOT NULL REFERENCES topics(id), "
                "PRIMARY KEY (article_id, topic_id))"
            )
        )
        conn.execute(text("INSERT INTO topics (id, name) VALUES (1, 'zeolites')"))
        conn.execute(text("INSERT INTO article_topics VALUES (7, 1)"))
        if with_new_tables:
            # What create_all leaves behind when the models are deployed before
            # this migration is: empty new tables, legacy data still in place.
            conn.execute(
                text("CREATE TABLE keywords (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL)")
            )
            conn.execute(
                text(
                    "CREATE TABLE article_keywords ("
                    "article_id INTEGER NOT NULL, keyword_id INTEGER NOT NULL, "
                    "PRIMARY KEY (article_id, keyword_id))"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE topic_keywords ("
                    "topic_id INTEGER NOT NULL REFERENCES topics(id), "
                    "keyword_id INTEGER NOT NULL REFERENCES keywords(id), "
                    "PRIMARY KEY (topic_id, keyword_id))"
                )
            )
    return engine


def _assert_migrated(conn) -> None:
    from sqlalchemy import text

    assert conn.execute(text("SELECT name FROM keywords")).scalars().all() == ["zeolites"]
    assert conn.execute(
        text("SELECT article_id, keyword_id FROM article_keywords")
    ).all() == [(7, 1)]


def test_legacy_topic_tables_become_the_keyword_tables(tmp_path) -> None:
    from app.db import _rename_legacy_keyword_tables

    engine = _legacy_database(tmp_path / "legacy.db")
    with engine.begin() as conn:
        _rename_legacy_keyword_tables(conn)
        _assert_migrated(conn)
        # Running again on the migrated database must change nothing.
        _rename_legacy_keyword_tables(conn)
        _assert_migrated(conn)


def test_legacy_data_still_moves_when_the_new_tables_exist_empty(tmp_path) -> None:
    """create_all can win the race and make the new tables before this runs."""
    from sqlalchemy import text

    from app.db import _rename_legacy_keyword_tables

    engine = _legacy_database(tmp_path / "half.db", with_new_tables=True)
    with engine.begin() as conn:
        _rename_legacy_keyword_tables(conn)
        _assert_migrated(conn)
        # The legacy tables are gone, so create_all can rebuild `topics` as the
        # grouping table.
        remaining = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
        assert "article_topics" not in remaining
        assert "topics" not in remaining
