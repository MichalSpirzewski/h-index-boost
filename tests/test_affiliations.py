import os

import fitz
import pytest

from app.ingest import extract_author_affiliations

# The base-14 "helv" font PyMuPDF writes by default is Latin-1 only, so a fixture
# containing "ł" or "ø" comes back out of the text extractor as "·" — an artefact
# of the test rig, not of any real PDF. Embed a Unicode TTF so author names in
# fixtures survive the round trip and read the way publisher PDFs actually do.
_UNICODE_FONT = next(
    (
        path
        for path in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/Library/Fonts/Arial Unicode.ttf",
        )
        if os.path.exists(path)
    ),
    None,
)
# Tests whose author names go beyond Latin-1 can only run with such a font.
needs_unicode_font = pytest.mark.skipif(
    _UNICODE_FONT is None, reason="no Unicode TTF on this system to embed in fixtures"
)


def _make_pdf(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    if _UNICODE_FONT is None:
        page.insert_text((72, 72), text)
    else:
        page.insert_text((72, 72), text, fontname="uni", fontfile=_UNICODE_FONT)
    return doc.tobytes()


def test_superscript_markers_map_per_author() -> None:
    # Two affiliations, marked a/b; author 1 belongs to both, author 2 to only a.
    pdf = _make_pdf(
        "A Paper Title\n"
        "Jakub Sierchula a,b, Michal Spirzewski a\n"
        "a National Centre for Nuclear Research, Otwock, Poland\n"
        "b Poznan University of Technology, Poznan, Poland\n"
        "Abstract\nBody text follows here."
    )
    affs = extract_author_affiliations(pdf, ["Jakub Sierchula", "Michal Spirzewski"])
    assert "National Centre for Nuclear Research" in affs[0]
    assert "Poznan University of Technology" in affs[0]
    assert "National Centre for Nuclear Research" in affs[1]
    assert "Poznan University of Technology" not in affs[1]


def test_marker_continuation_captures_foreign_language_affiliations() -> None:
    # Regression: a Czech "b Fakulta…"/"c Centrum…" line carries no English
    # keyword, but once inside the affiliation block it must still be picked up —
    # otherwise its authors wrongly inherit affiliation "a".
    pdf = _make_pdf(
        "A Paper Title\n"
        "Michal Spirzewski a, Michal Volf b, Tomas Melichar c\n"
        "a National Centre for Nuclear Research, Otwock, Poland\n"
        "b Fakulta Strojni Zapadoceske Univerzity v Plzni, Plzen, Czech Republic\n"
        "c Centrum vyzkumu Rez s.r.o., Rez, Czech Republic\n"
        "Abstract\nBody."
    )
    affs = extract_author_affiliations(
        pdf, ["Michal Spirzewski", "Michal Volf", "Tomas Melichar"]
    )
    assert "National Centre for Nuclear Research" in affs[0]
    assert "Fakulta" in affs[1] and "National Centre" not in affs[1]
    assert "Centrum" in affs[2] and "National Centre" not in affs[2]


def test_single_affiliation_shared_by_all_authors() -> None:
    pdf = _make_pdf(
        "A Paper Title\n"
        "Alice Adams, Bob Baker, Carol Clark\n"
        "National Centre for Nuclear Research, Otwock, Poland\n"
        "Abstract\nBody."
    )
    affs = extract_author_affiliations(pdf, ["Alice Adams", "Bob Baker", "Carol Clark"])
    assert affs == ["National Centre for Nuclear Research, Otwock, Poland"] * 3


def test_no_affiliation_block_returns_none() -> None:
    pdf = _make_pdf("A Paper Title\nAlice Adams\nAbstract\nBody with no affiliations.")
    assert extract_author_affiliations(pdf, ["Alice Adams"]) == [None]


def test_empty_author_list() -> None:
    pdf = _make_pdf("A Paper Title\nDept of Physics, Some University\nAbstract\nBody.")
    assert extract_author_affiliations(pdf, []) == []


def test_academic_editor_masthead_is_not_an_affiliation() -> None:
    # Regression: MDPI front matter starts with "Academic Editor: …", which used
    # to match the "Academ" stem, open the affiliation block on the masthead and
    # then stop it at the "Article" heading — losing the real affiliation below.
    pdf = _make_pdf(
        "Academic Editor: Francesco Nocera\n"
        "Citation: Spirzewski, M.; Nowak,\n"
        "M.M. A Similarity-Based Scaling\n"
        "18, 5935. https://doi.org/10.3390/\n"
        "Article\n"
        "A Similarity-Based Scaling Methodology\n"
        "Michal Spirzewski and Mateusz Nowak\n"
        "National Centre for Nuclear Research, Otwock-Swierk, Poland\n"
        "Abstract\nBody text."
    )
    affs = extract_author_affiliations(pdf, ["Michal Spirzewski", "Mateusz Nowak"])
    assert all(a == "National Centre for Nuclear Research, Otwock-Swierk, Poland" for a in affs)


def test_academy_of_sciences_still_counts_as_an_affiliation() -> None:
    # The narrowed pattern must keep matching genuine academies.
    pdf = _make_pdf(
        "A Paper Title\nJan Kowalski\n"
        "Polish Academy of Sciences, Warsaw, Poland\n"
        "Abstract\nBody."
    )
    assert extract_author_affiliations(pdf, ["Jan Kowalski"]) == [
        "Polish Academy of Sciences, Warsaw, Poland"
    ]


def test_title_wording_is_not_mistaken_for_an_affiliation() -> None:
    """Regression: "reliability-centered" fired the Center stem, so the title became
    affiliation #1. That also emptied the author region above it, which broke the
    marker mapping and gave every author every affiliation on the paper."""
    pdf = _make_pdf(
        "Integrated framework for the reliability-centered design of advanced\n"
        "nuclear facilities\n"
        "Karol Kowal a\n"
        ", Elzbieta Jartych c\n"
        ", Hirofumi Ohashi d\n"
        "a National Centre for Nuclear Research (NCBJ), Otwock, Poland\n"
        "c Lublin University of Technology (LUT), Lublin, Poland\n"
        "d Japan Atomic Energy Agency (JAEA), Tokyo, Japan\n"
        "A B S T R A C T\nBody."
    )
    affs = extract_author_affiliations(
        pdf, ["Karol Kowal", "Elzbieta Jartych", "Hirofumi Ohashi"]
    )
    assert "National Centre" in affs[0] and "Lublin" not in affs[0]
    assert affs[1] == "Lublin University of Technology (LUT), Lublin, Poland"
    assert affs[2] == "Japan Atomic Energy Agency (JAEA), Tokyo, Japan"
    assert not any("Integrated framework" in a for a in affs)


@needs_unicode_font
def test_separate_marker_lines_map_each_author_to_the_right_affiliation() -> None:
    """Elsevier PDFs can extract superscripts as standalone lines."""
    pdf = _make_pdf(
        "Impact of equipment reliability on safety classification of research reactors\n"
        "Jacek Kałowski a, Karol Kowal a, Rafał Laskowski b, Grzegorz Mrugała a\n"
        "a\n"
        "National Centre for Nuclear Research (NCBJ), Otwock, Poland\n"
        "b\n"
        "Warsaw University of Technology (WUT), Warsaw, Poland\n"
        "A B S T R A C T\nBody."
    )

    affs = extract_author_affiliations(
        pdf,
        ["Jacek Kałowski", "Karol Kowal", "Rafał Laskowski", "Grzegorz Mrugała"],
    )

    assert "NCBJ" in affs[0]
    assert "NCBJ" in affs[1]
    assert affs[2] == "Warsaw University of Technology (WUT), Warsaw, Poland"
    assert "NCBJ" in affs[3]
    assert not any("Impact of equipment" in affiliation for affiliation in affs)


def test_detached_accent_in_header_still_matches_the_crossref_name() -> None:
    """Regression: Elsevier renders "Prusiński" as "Prusi´nski" — a spacing acute
    plus a bare "n" — while Crossref stores the precomposed form. A literal
    substring search finds no markers, so the author was left with no affiliation
    at all while his ASCII-named co-author mapped fine."""
    pdf = _make_pdf(
        "Thermal characteristics of developing flow in an annular pipe\n"
        "Piotr Prusi´nski a,*, Sławomir Kubacki b\n"
        "a National Centre for Nuclear Research, Andrzeja Soltana 7, Otwock, Poland\n"
        "b Warsaw University of Technology, Institute of Aeronautics, Warsaw, Poland\n"
        "A B S T R A C T\nBody."
    )

    affs = extract_author_affiliations(pdf, ["Piotr Prusiński", "Sławomir Kubacki"])

    assert affs[0] is not None and "National Centre for Nuclear Research" in affs[0]
    assert "Warsaw University" not in affs[0]
    assert affs[1] is not None and "Warsaw University of Technology" in affs[1]


@needs_unicode_font
def test_precomposed_accents_and_non_decomposing_letters_still_match() -> None:
    """The fold must not break names that already agree between PDF and Crossref.
    Both surnames carry a letter NFKD leaves untouched — "ś" decomposes, "ø" does
    not — so each exercises a different branch of the fold."""
    pdf = _make_pdf(
        "A Paper Title\n"
        "Anna Wiśniewska a, Lars Sørensen b\n"
        "a National Centre for Nuclear Research, Otwock, Poland\n"
        "b Institute for Energy Technology, Kjeller, Norway\n"
        "Abstract\nBody."
    )

    affs = extract_author_affiliations(pdf, ["Anna Wiśniewska", "Lars Sørensen"])

    assert affs[0] is not None and "National Centre" in affs[0]
    assert "Institute for Energy" not in affs[0]
    assert affs[1] is not None and "Institute for Energy Technology" in affs[1]


@needs_unicode_font
def test_ascii_stripped_surname_in_header_matches_the_accented_crossref_name() -> None:
    """Some publishers set the header with diacritics stripped entirely while
    Crossref keeps them. "ś" folds via NFKD, "ø" only via the explicit letter
    table — without both, these authors get no affiliation."""
    pdf = _make_pdf(
        "A Paper Title\n"
        "Anna Wisniewska a, Lars Sorensen b\n"
        "a National Centre for Nuclear Research, Otwock, Poland\n"
        "b Institute for Energy Technology, Kjeller, Norway\n"
        "Abstract\nBody."
    )

    affs = extract_author_affiliations(pdf, ["Anna Wiśniewska", "Lars Sørensen"])

    # The negative halves carry this test: with no name match at all, the parser
    # falls back to sharing every affiliation with everyone, which would satisfy
    # the positive assertions on its own.
    assert affs[0] is not None and "National Centre" in affs[0]
    assert "Institute for Energy" not in affs[0]
    assert affs[1] is not None and "Institute for Energy Technology" in affs[1]
    assert "National Centre" not in affs[1]


def test_centre_and_center_still_match_as_institutions() -> None:
    """Narrowing the stem must not cost us real centres."""
    for line in (
        "National Centre for Nuclear Research, Otwock, Poland",
        "Center for Advanced Studies, Warsaw, Poland",
    ):
        pdf = _make_pdf(f"A Title\nJan Kowalski\n{line}\nAbstract\nBody.")
        assert extract_author_affiliations(pdf, ["Jan Kowalski"]) == [line]
