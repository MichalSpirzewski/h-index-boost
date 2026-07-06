import copy


def _ingest_with_authors(client, monkeypatch, base_message, doi, families):
    """Ingest one article whose Crossref response has the given author families."""
    from app import ingest

    message = copy.deepcopy(base_message)
    message["DOI"] = doi
    message["title"] = [f"Paper by {families[0]}"]
    message["author"] = [
        {"given": "Alex", "family": family, "sequence": "first" if i == 0 else "additional"}
        for i, family in enumerate(families)
    ]
    monkeypatch.setattr(ingest, "fetch_crossref", lambda _doi: message)
    resp = client.post("/api/ingest", data={"doi": doi})
    assert resp.json()["status"] == "created"


def test_dashboard_lists_first_three_authors_with_et_al(
    client, monkeypatch, crossref_message
) -> None:
    _ingest_with_authors(
        client, monkeypatch, crossref_message,
        "10.9999/many-authors", ["Adams", "Baker", "Clark", "Davis", "Evans"],
    )
    page = client.get("/").text
    assert "Alex Adams, Alex Baker, Alex Clark et al." in page
    assert "Davis" not in page  # only the first three are shown


def test_dashboard_sort_by_author(client, monkeypatch, crossref_message) -> None:
    # Zebra added first (most recent-last), Aardvark second (most recent-first)
    _ingest_with_authors(client, monkeypatch, crossref_message, "10.9999/z", ["Zebra"])
    _ingest_with_authors(client, monkeypatch, crossref_message, "10.9999/a", ["Aardvark"])

    recent = client.get("/").text
    assert recent.index("Aardvark") < recent.index("Zebra")  # newest first

    by_author = client.get("/?sort=author").text
    assert by_author.index("Aardvark") < by_author.index("Zebra")

    # reversed insertion order must still sort alphabetically
    _ingest_with_authors(client, monkeypatch, crossref_message, "10.9999/m", ["Miller"])
    by_author = client.get("/?sort=author").text
    assert (
        by_author.index("Aardvark") < by_author.index("Miller") < by_author.index("Zebra")
    )
