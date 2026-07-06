import json

from app.bibtex import article_bibtex, escape_bibtex, export_bibtex, make_cite_key
from app.models import Article, ArticleAuthor, Author


def _article_from_fixture(crossref_message) -> Article:
    return Article(id=1, doi=crossref_message["DOI"], crossref_json=json.dumps(crossref_message))


def test_escape_special_characters() -> None:
    assert escape_bibtex("50% of {things} & #tags") == r"50\% of \{things\} \& \#tags"


def test_unicode_is_kept() -> None:
    assert escape_bibtex("Schrödinger–Æther") == "Schrödinger–Æther"


def test_cite_key_format() -> None:
    assert make_cite_key("Smith", 2009, "Measured measurement") == "smith2009measured"
    # unicode last names are ascii-folded, missing parts get placeholders
    assert make_cite_key("Müller", 2020, "Über alles") == "muller2020uber"
    assert make_cite_key(None, None, None) == "anonnd"


def test_field_mapping_from_crossref_json(crossref_message) -> None:
    entry = article_bibtex(_article_from_fixture(crossref_message))
    assert entry.startswith("@article{smith2009measured,")
    assert "author = {Alice B. Smith and Carol Danvers}" in entry
    assert "title = {Measured measurement}" in entry
    assert "journal = {Nature Physics}" in entry
    assert "year = {2009}" in entry
    assert "volume = {5}" in entry
    assert "number = {4}" in entry
    assert "pages = {243-244}" in entry
    assert "doi = {10.1038/nphys1170}" in entry


def test_cite_key_dedup_within_export(crossref_message) -> None:
    a1 = _article_from_fixture(crossref_message)
    a2 = _article_from_fixture(crossref_message)
    a2.id = 2
    bib = export_bibtex([a1, a2])
    assert "@article{smith2009measured," in bib
    assert "@article{smith2009measureda," in bib


def test_fallback_without_crossref_json() -> None:
    article = Article(id=3, title="A Stub Paper", year=2021, journal="Preprints")
    author = Author(id=1, full_name="Jane van Dyke")
    article.author_links = [ArticleAuthor(article_id=3, author_id=1, position=0, author=author)]
    entry = article_bibtex(article)
    # v1 uses the last whitespace token as the surname ("van Dyke" -> "dyke")
    assert entry.startswith("@misc{dyke2021a,")
    assert "author = {Jane van Dyke}" in entry
    assert "title = {A Stub Paper}" in entry
