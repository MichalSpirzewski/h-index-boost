"""Publication dates: Crossref gives whatever precision the publisher used."""

import copy
import json

from sqlalchemy import select

from app.ingest import backfill_dates, crossref_dates, iso_date_from_parts


def test_iso_date_keeps_the_precision_it_was_given() -> None:
    assert iso_date_from_parts([2025, 11, 11]) == "2025-11-11"
    assert iso_date_from_parts([2026, 5]) == "2026-05"
    assert iso_date_from_parts([2022]) == "2022"


def test_iso_date_zero_pads_so_string_order_is_chronological() -> None:
    # The whole scheme rests on this: "2026-05" must sort before "2026-12".
    assert iso_date_from_parts([2026, 5]) == "2026-05"
    assert iso_date_from_parts([2026, 5]) < iso_date_from_parts([2026, 12])
    assert iso_date_from_parts([2025, 12]) < iso_date_from_parts([2026, 1])
    assert iso_date_from_parts([2026]) < iso_date_from_parts([2026, 1])


def test_iso_date_handles_missing_and_empty() -> None:
    assert iso_date_from_parts(None) is None
    assert iso_date_from_parts([]) is None


def test_month_only_issue_is_not_padded_to_a_day(crossref_message) -> None:
    """An Elsevier issue dated 'May 2026' has no day; inventing one would be a lie."""
    crossref_message["issued"] = {"date-parts": [[2026, 5]]}
    published, _ = crossref_dates(crossref_message)
    assert published == "2026-05"
    assert not published.endswith("-01")


def test_published_comes_from_issued_and_online_from_created(crossref_message) -> None:
    crossref_message["issued"] = {"date-parts": [[2026, 5]]}
    crossref_message["created"] = {"date-parts": [[2026, 5, 21]]}
    assert crossref_dates(crossref_message) == ("2026-05", "2026-05-21")


def test_published_online_wins_over_created(crossref_message) -> None:
    crossref_message["issued"] = {"date-parts": [[2025, 11, 11]]}
    crossref_message["published-online"] = {"date-parts": [[2025, 11, 11]]}
    crossref_message["created"] = {"date-parts": [[2025, 11, 30]]}
    _, online = crossref_dates(crossref_message)
    assert online == "2025-11-11"


def test_issue_date_is_not_replaced_by_the_online_date(crossref_message) -> None:
    """The two are genuinely different dates and must not collapse into one."""
    crossref_message["issued"] = {"date-parts": [[2026, 12]]}
    crossref_message["created"] = {"date-parts": [[2026, 7, 1]]}
    published, online = crossref_dates(crossref_message)
    assert published == "2026-12"
    assert online == "2026-07-01"


def test_ingest_stores_both_dates(client, monkeypatch, crossref_message) -> None:
    from app import db, ingest
    from app.models import Article

    message = copy.deepcopy(crossref_message)
    message["issued"] = {"date-parts": [[2026, 4]]}
    message["created"] = {"date-parts": [[2026, 3, 23]]}
    monkeypatch.setattr(ingest, "fetch_crossref", lambda _doi: message)

    body = client.post("/api/ingest", data={"doi": "10.9999/dates"}).json()
    with db.SessionLocal() as session:
        article = session.get(Article, body["article_id"])
        assert article.published_date == "2026-04"
        assert article.online_date == "2026-03-23"
        assert article.year == 2026  # still there; BibTeX and cite keys use it


def test_backfill_fills_dates_from_stored_json_without_refetching(client) -> None:
    """Articles ingested before the columns existed: the JSON on disk is enough."""
    from app import db
    from app.models import Article

    message = {
        "DOI": "10.9999/old",
        "title": ["An Older Paper"],
        "issued": {"date-parts": [[2021, 7]]},
        "created": {"date-parts": [[2021, 3, 24]]},
    }
    with db.SessionLocal() as session:
        session.add(
            Article(doi="10.9999/old", year=2021, crossref_json=json.dumps(message))
        )
        session.commit()

    with db.SessionLocal() as session:
        assert backfill_dates(session) == 1
    with db.SessionLocal() as session:
        article = session.scalar(select(Article).where(Article.doi == "10.9999/old"))
        assert article.published_date == "2021-07"
        assert article.online_date == "2021-03-24"

    with db.SessionLocal() as session:
        assert backfill_dates(session) == 0  # idempotent


def test_backfill_never_overwrites_an_existing_date(client) -> None:
    from app import db
    from app.models import Article

    message = {"issued": {"date-parts": [[2021, 7]]}}
    with db.SessionLocal() as session:
        session.add(
            Article(
                doi="10.9999/manual",
                crossref_json=json.dumps(message),
                published_date="1999-01-01",
                online_date="1999-01-01",
            )
        )
        session.commit()

    with db.SessionLocal() as session:
        assert backfill_dates(session) == 0
