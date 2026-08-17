"""Title and author extraction from a front page's typography.

The PDFs here are laid out the way a publisher lays one out — the title in the
largest face, affiliation markers a few points smaller and raised — because that
layout, not the flat text, is what the parser reads.
"""

import fitz

from app.ingest import (
    extract_author_affiliations,
    extract_authors_from_pdf,
    extract_journal_from_pdf,
    extract_keywords_from_pdf,
    extract_title_from_pdf,
    pdf_title,
)


def _make_pdf(rows: list[list[tuple[str, float]]], metadata: dict | None = None) -> bytes:
    """One page built from rows of (text, fontsize) pieces.

    Every piece of a row shares one baseline, and a piece smaller than the row's
    largest size is raised — which is how a superscript marker is typeset, and how
    it ends up flagged as one in the extracted spans.
    """
    doc = fitz.open()
    # Wider than A4: insert_text() does not wrap, and the real title lines below
    # would be clipped mid-word at this font size on a narrower page.
    page = doc.new_page(width=900)
    y = 80.0
    for row in rows:
        largest = max(size for _text, size in row)
        x = 60.0
        for text, size in row:
            page.insert_text((x, y - (3 if size < largest else 0)), text, fontsize=size)
            x += fitz.get_text_length(text, fontsize=size)
        y += largest + 12
    if metadata:
        doc.set_metadata(metadata)
    return doc.tobytes()


_TITLE_LINES = [
    "Computer codes in the safety analysis for nuclear power plants.",
    "Computational capabilities of thermal-hydraulic tools, using the example of",
    "the RELAP5 code",
]
_TITLE = " ".join(_TITLE_LINES)


def _journal_paper(**overrides) -> bytes:
    """The shape of a TeX-produced journal article: masthead, oversized title,
    byline with fused markers, marked affiliations, abstract, wrapped keywords."""
    rows = [
        [("Open Access Journal", 9.5)],
        [("Journal of Power Technologies 94 (Nuclear Issue) (2014) 41-50", 12.0)],
        [("journal homepage:papers.itc.pw.edu.pl", 8.0)],
        *[[(line, 17.2)] for line in _TITLE_LINES],
        [
            ("Eleonora Skrzypek", 12.0),
            ("*,a,b", 8.0),
            (", Maciej Skrzypek", 12.0),
            ("a,b", 8.0),
        ],
        [("aInstitute of Heat Engineering, Warsaw University of Technology", 9.0)],
        [("Nowowiejska 21/25, 00-665, Warsaw, Poland", 9.0)],
        [("bNational Center for Nuclear Research (NCBJ)", 9.0)],
        [("A. Soltana 7, 05-400, Otwock-Swierk, Poland", 9.0)],
        [("Abstract", 11.0)],
        [("Safety is a paramount concern of the Nuclear Power Program in Poland.", 9.0)],
        [
            (
                "Keywords: Safety analysis, Neutronic analysis, "
                "Thermal-hydraulic analysis, Severe accident, CFD,",
                9.0,
            )
        ],
        [("RELAP5, MELCOR", 9.0)],
        [("1. Engineering computer codes for nuclear power applications", 11.0)],
    ]
    return _make_pdf(rows, **overrides)


def test_title_is_the_largest_face_however_many_lines_it_wraps_onto() -> None:
    assert extract_title_from_pdf(_journal_paper()) == _TITLE


def test_a_masthead_set_as_large_as_the_title_is_not_the_title() -> None:
    pdf = _make_pdf(
        [
            [("Open Access Journal", 17.2)],
            [("Reactor Kinetics Under Load Following", 17.2)],
            [("Alice Adams", 12.0)],
        ]
    )
    assert extract_title_from_pdf(pdf) == "Reactor Kinetics Under Load Following"


def test_a_same_size_heading_further_down_does_not_extend_the_title() -> None:
    pdf = _make_pdf(
        [
            [("Reactor Kinetics Under Load Following", 17.2)],
            [("Alice Adams", 12.0)],
            [("1. Introduction", 17.2)],
        ]
    )
    assert extract_title_from_pdf(pdf) == "Reactor Kinetics Under Load Following"


def test_metadata_title_is_preferred_and_the_page_is_the_fallback() -> None:
    assert pdf_title(_journal_paper(metadata={"title": "A Title Set In Metadata"})) == (
        "A Title Set In Metadata"
    )
    # TeX writes no metadata title, which is why the page scan exists at all.
    assert pdf_title(_journal_paper()) == _TITLE


def test_a_page_with_nothing_title_shaped_yields_none() -> None:
    assert extract_title_from_pdf(_make_pdf([[("Short", 12.0)]])) is None
    assert extract_title_from_pdf(b"not a pdf") is None


def test_byline_drops_superscript_markers_fused_onto_surnames() -> None:
    # Flat text extraction returns "Maciej Skrzypeka,b"; the spans keep the
    # marker separate, so the parser must read those instead.
    assert "Skrzypeka" in fitz.open(
        stream=_journal_paper(), filetype="pdf"
    )[0].get_text()
    assert extract_authors_from_pdf(_journal_paper()) == [
        "Eleonora Skrzypek",
        "Maciej Skrzypek",
    ]


def test_byline_stops_before_the_affiliations() -> None:
    pdf = _make_pdf(
        [
            [("Reactor Kinetics Under Load Following", 17.2)],
            [("Institute of Heat Engineering, Warsaw University of Technology", 9.0)],
        ]
    )
    assert extract_authors_from_pdf(pdf) == []


def test_byline_stops_at_a_contact_address() -> None:
    pdf = _make_pdf(
        [
            [("Reactor Kinetics Under Load Following", 17.2)],
            [("eleonora.skrzypek@itc.pw.edu.pl", 9.0)],
        ]
    )
    assert extract_authors_from_pdf(pdf) == []


def test_byline_keeps_lowercase_name_particles() -> None:
    pdf = _make_pdf(
        [
            [("Reactor Kinetics Under Load Following", 17.2)],
            [("Piet van der Meer, Alice Adams", 12.0)],
        ]
    )
    assert extract_authors_from_pdf(pdf) == ["Piet van der Meer", "Alice Adams"]


def test_no_authors_when_there_is_no_title_to_anchor_the_byline() -> None:
    assert extract_authors_from_pdf(b"not a pdf") == []


def test_keywords_continue_onto_the_line_the_column_width_broke() -> None:
    assert extract_keywords_from_pdf(_journal_paper()) == [
        "Safety analysis",
        "Neutronic analysis",
        "Thermal-hydraulic analysis",
        "Severe accident",
        "CFD",
        "RELAP5",
        "MELCOR",
    ]


def _masthead(line: str) -> bytes:
    """A page whose only front matter is `line`, above a title and a byline."""
    return _make_pdf(
        [
            [(line, 12.0)],
            [("Reactor Kinetics Under Load Following", 17.2)],
            [("Alice Adams", 12.0)],
        ]
    )


def test_journal_name_comes_from_the_running_head_above_the_title() -> None:
    assert extract_journal_from_pdf(_journal_paper()) == "Journal of Power Technologies"


def test_journal_name_stops_at_the_volume_year_or_page_apparatus() -> None:
    cases = {
        # Elsevier: name, volume, year, article number.
        "Nuclear Engineering and Design 380 (2021) 111234": (
            "Nuclear Engineering and Design"
        ),
        # MDPI: a single-word name, then year, volume, article number.
        "Energies 2023, 16, 4567": "Energies",
        # Bare name and volume, nothing else.
        "Progress in Nuclear Energy 145": "Progress in Nuclear Energy",
        # An ampersand is spelled out, as it is on the Crossref path.
        "Science & Technology 12 (2020) 1-9": "Science and Technology",
    }
    for line, expected in cases.items():
        assert extract_journal_from_pdf(_masthead(line)) == expected, line


def test_masthead_noise_is_never_read_as_the_journal() -> None:
    for line in (
        "journal homepage:papers.itc.pw.edu.pl",
        "Open Access Journal 94 (2014)",
        "https://doi.org/10.1016/j.nucengdes.2021.111234",
        "Received 12 March 2014",
        "© 2014 The Authors",
        "Downloaded 3 times",
    ):
        assert extract_journal_from_pdf(_masthead(line)) is None, line


def test_a_masthead_with_no_citation_numbers_is_not_guessed_at() -> None:
    # Without a volume/year/page the line cannot be told from any other text,
    # so nothing is claimed rather than something wrong.
    assert extract_journal_from_pdf(_masthead("Nature Physics")) is None


def test_the_journal_is_only_looked_for_above_the_title() -> None:
    pdf = _make_pdf(
        [
            [("Reactor Kinetics Under Load Following", 17.2)],
            [("Alice Adams", 12.0)],
            [("Nuclear Engineering and Design 380 (2021) 111234", 9.0)],
        ]
    )
    assert extract_journal_from_pdf(pdf) is None


def test_no_journal_from_an_unreadable_file() -> None:
    assert extract_journal_from_pdf(b"not a pdf") is None


_EXPECTED_KEYWORDS = [
    "Safety analysis",
    "Neutronic analysis",
    "Thermal-hydraulic analysis",
    "Severe accident",
    "CFD",
    "RELAP5",
    "MELCOR",
]


def test_ingesting_a_doi_less_paper_stores_title_authors_and_keywords(client) -> None:
    created = client.post(
        "/api/ingest",
        files={"file": ("paper.pdf", _journal_paper(), "application/pdf")},
    ).json()
    assert created["doi"] is None

    detail = client.get(f"/api/articles/{created['article_id']}").json()
    assert detail["title"] == _TITLE
    assert detail["authors"] == ["Eleonora Skrzypek", "Maciej Skrzypek"]
    assert detail["keywords"] == _EXPECTED_KEYWORDS
    assert detail["journal"] == "Journal of Power Technologies"


def test_rescan_backfills_a_record_stored_before_the_front_page_was_parsed(client) -> None:
    from app import db, ingest
    from app.models import Article

    # What such a record looks like: a PDF on disk, an abstract, some of the
    # keywords, and no title or authors at all.
    with db.SessionLocal() as session:
        article = Article(
            status="ready", pdf_path=ingest.save_pdf(_journal_paper(), None)
        )
        session.add(article)
        session.commit()
        article_id = article.id

    client.post(f"/articles/{article_id}/rescan")

    detail = client.get(f"/api/articles/{article_id}").json()
    assert detail["title"] == _TITLE
    assert detail["authors"] == ["Eleonora Skrzypek", "Maciej Skrzypek"]
    assert detail["keywords"] == _EXPECTED_KEYWORDS
    assert detail["journal"] == "Journal of Power Technologies"
    # The journal page it now links to lists it.
    assert _TITLE in client.get("/journals/Journal of Power Technologies").text


def test_a_paper_with_crossref_metadata_keeps_crossrefs_author_list(client) -> None:
    """Crossref owns the byline where it has one — the PDF must not add to it."""
    created = client.post(
        "/api/ingest",
        data={"doi": "10.1038/nphys1170"},
        files={"file": ("paper.pdf", _journal_paper(), "application/pdf")},
    ).json()

    detail = client.get(f"/api/articles/{created['article_id']}").json()
    assert detail["title"] == "Measured measurement"
    assert detail["authors"] == ["Alice B. Smith", "Carol Danvers"]
    assert "Skrzypek" not in " ".join(detail["authors"])
    # Crossref's container title likewise stands, not the PDF's running head.
    assert detail["journal"] == "Nature Physics"


def test_affiliations_survive_markers_fused_to_the_institution() -> None:
    # "aInstitute…"/"bNational…" (the marker fused by extraction) map to a and b;
    # both authors carry both markers, so both get both institutions.
    affiliations = extract_author_affiliations(
        _journal_paper(), ["Eleonora Skrzypek", "Maciej Skrzypek"]
    )
    assert affiliations[0] == affiliations[1]
    assert "Institute of Heat Engineering" in affiliations[0]
    assert "National Center for Nuclear Research" in affiliations[0]
    # The street address continuing an affiliation is not an institution of its own.
    assert "Soltana" not in affiliations[0]
