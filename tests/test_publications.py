from sqlalchemy import select, text


def test_publication_subtypes_round_trip_polymorphically(client) -> None:
    from app import db
    from app.models import ConferencePublication, JournalPublication, Publication

    with db.SessionLocal() as session:
        journal = JournalPublication(
            title="Journal result",
            journal="Nature Physics",
            status="ready",
        )
        conference = ConferencePublication(
            title="Conference result",
            conference_name="International Conference on Research Software",
            proceedings_title="Proceedings of ICRS 2026",
            conference_location="Warsaw, Poland",
            conference_start_date="2026-09-14",
            conference_end_date="2026-09-16",
            status="ready",
        )
        session.add_all([journal, conference])
        session.commit()
        journal_id, conference_id = journal.id, conference.id

    with db.SessionLocal() as session:
        publications = {
            publication.id: publication
            for publication in session.scalars(select(Publication)).all()
        }

        stored_journal = publications[journal_id]
        assert isinstance(stored_journal, JournalPublication)
        assert stored_journal.publication_type == "journal"
        assert stored_journal.venue_name == "Nature Physics"

        stored_conference = publications[conference_id]
        assert isinstance(stored_conference, ConferencePublication)
        assert stored_conference.publication_type == "conference"
        assert stored_conference.venue_name == "International Conference on Research Software"
        assert stored_conference.proceedings_title == "Proceedings of ICRS 2026"


def test_article_name_remains_a_compatible_publication_alias(client) -> None:
    from app.models import Article, Publication

    assert Article is Publication
    article = Article(title="Legacy caller", status="ready")
    assert isinstance(article, Publication)
    assert article.publication_type == "publication"


def test_publication_migration_columns_exist(client) -> None:
    from app import db

    with db.engine.connect() as connection:
        columns = {
            row[1] for row in connection.execute(text("PRAGMA table_info(articles)"))
        }

    assert {
        "publication_type",
        "conference_name",
        "proceedings_title",
        "conference_location",
        "conference_start_date",
        "conference_end_date",
    } <= columns


def test_existing_status_api_exposes_publication_metadata(client) -> None:
    from app import db
    from app.main import article_status
    from app.models import ConferencePublication

    with db.SessionLocal() as session:
        conference = ConferencePublication(
            title="API-visible conference paper",
            conference_name="Research Software Conference",
            proceedings_title="RSC Proceedings",
            status="ready",
        )
        session.add(conference)
        session.commit()

        payload = article_status(conference.id, session)

    assert payload["publication_type"] == "conference"
    assert payload["conference_name"] == "Research Software Conference"
    assert payload["proceedings_title"] == "RSC Proceedings"
    assert payload["venue_name"] == "Research Software Conference"
