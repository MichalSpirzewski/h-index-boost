import fitz

from app.ingest import extract_author_affiliations


def _make_pdf(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
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
