from sqlalchemy import text


def test_wal_mode_enabled(client) -> None:
    from app import db

    with db.engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"


def test_fts5_search_stays_in_sync(client) -> None:
    from app import db
    from app.models import Article

    with db.SessionLocal() as session:
        article = Article(title="Zeolite catalysis review", journal="ACS Catalysis", status="ready")
        session.add(article)
        session.commit()
        assert db.search_article_ids(session, "zeolite") == [article.id]

        article.title = "Perovskite solar cells"
        session.commit()
        assert db.search_article_ids(session, "zeolite") == []
        assert db.search_article_ids(session, "perovskite") == [article.id]

        session.delete(article)
        session.commit()
        assert db.search_article_ids(session, "perovskite") == []
