import json
from xml.etree import ElementTree as ET

from app.models import Article, ArticleAuthor, Author
from app.word_xml import NS, article_word_xml, export_word_xml, split_name, xml_filename


def _article_from_fixture(crossref_message) -> Article:
    return Article(id=1, doi=crossref_message["DOI"], crossref_json=json.dumps(crossref_message))


def _b(element: ET.Element, name: str) -> ET.Element | None:
    return element.find(f"{{{NS}}}{name}")


def _text(element: ET.Element, name: str) -> str | None:
    found = _b(element, name)
    return found.text if found is not None else None


def _sources(xml: str) -> list[ET.Element]:
    return ET.fromstring(xml).findall(f"{{{NS}}}Source")


def test_output_is_well_formed_and_namespaced(crossref_message) -> None:
    xml = article_word_xml(_article_from_fixture(crossref_message))
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8" standalone="no"?>')
    root = ET.fromstring(xml)
    assert root.tag == f"{{{NS}}}Sources"


def test_field_mapping_from_crossref_json(crossref_message) -> None:
    (source,) = _sources(article_word_xml(_article_from_fixture(crossref_message)))
    assert _text(source, "Tag") == "smith2009measured"
    assert _text(source, "SourceType") == "JournalArticle"
    assert _text(source, "Title") == "Measured measurement"
    assert _text(source, "JournalName") == "Nature Physics"
    assert _text(source, "Year") == "2009"
    assert _text(source, "Volume") == "5"
    assert _text(source, "Issue") == "4"
    assert _text(source, "Pages") == "243-244"
    assert _text(source, "DOI") == "10.1038/nphys1170"


def test_authors_are_split_into_last_and_first(crossref_message) -> None:
    (source,) = _sources(article_word_xml(_article_from_fixture(crossref_message)))
    name_list = source.find(f"{{{NS}}}Author/{{{NS}}}Author/{{{NS}}}NameList")
    people = name_list.findall(f"{{{NS}}}Person")
    assert [(_text(p, "Last"), _text(p, "First")) for p in people] == [
        ("Smith", "Alice B."),
        ("Danvers", "Carol"),
    ]


def test_source_type_falls_back_to_misc(crossref_message) -> None:
    crossref_message["type"] = "posted-content"
    (source,) = _sources(article_word_xml(_article_from_fixture(crossref_message)))
    assert _text(source, "SourceType") == "Misc"


def test_page_dashes_are_normalised(crossref_message) -> None:
    crossref_message["page"] = "243–244"  # en dash, as some publishers send it
    (source,) = _sources(article_word_xml(_article_from_fixture(crossref_message)))
    assert _text(source, "Pages") == "243-244"


def test_special_characters_are_escaped_by_elementtree(crossref_message) -> None:
    crossref_message["title"] = ["Measurement & <weird> consequences"]
    xml = article_word_xml(_article_from_fixture(crossref_message))
    assert "&amp;" in xml and "&lt;weird&gt;" in xml
    (source,) = _sources(xml)
    assert _text(source, "Title") == "Measurement & <weird> consequences"


def test_unicode_is_kept(crossref_message) -> None:
    crossref_message["author"] = [{"given": "Erwin", "family": "Schrödinger"}]
    (source,) = _sources(article_word_xml(_article_from_fixture(crossref_message)))
    person = source.find(f"{{{NS}}}Author/{{{NS}}}Author/{{{NS}}}NameList/{{{NS}}}Person")
    assert _text(person, "Last") == "Schrödinger"


def test_tag_dedup_within_export(crossref_message) -> None:
    a1 = _article_from_fixture(crossref_message)
    a2 = _article_from_fixture(crossref_message)
    a2.id = 2
    tags = [_text(s, "Tag") for s in _sources(export_word_xml([a1, a2]))]
    assert tags == ["smith2009measured", "smith2009measureda"]


def test_tag_matches_the_bibtex_cite_key(crossref_message) -> None:
    from app.bibtex import article_bibtex

    article = _article_from_fixture(crossref_message)
    (source,) = _sources(article_word_xml(article))
    assert f"@article{{{_text(source, 'Tag')}," in article_bibtex(article)


def test_fallback_without_crossref_json() -> None:
    article = Article(id=3, title="A Stub Paper", year=2021, journal="Preprints")
    author = Author(id=1, full_name="Jane van Dyke")
    article.author_links = [ArticleAuthor(article_id=3, author_id=1, position=0, author=author)]
    (source,) = _sources(article_word_xml(article))
    assert _text(source, "Tag") == "dyke2021a"
    assert _text(source, "SourceType") == "Misc"
    assert _text(source, "Title") == "A Stub Paper"
    person = source.find(f"{{{NS}}}Author/{{{NS}}}Author/{{{NS}}}NameList/{{{NS}}}Person")
    assert (_text(person, "Last"), _text(person, "First")) == ("Dyke", "Jane van")


def test_split_name_edge_cases() -> None:
    assert split_name("Jane Doe") == ("Doe", "Jane")
    assert split_name("Madonna") == ("Madonna", "")
    assert split_name("") == ("", "")


def test_download_filename(crossref_message) -> None:
    assert xml_filename(_article_from_fixture(crossref_message)) == "smith2009measured.xml"
