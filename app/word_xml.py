"""Microsoft Word Source Manager XML generation from stored Crossref JSON.

Word (References -> Manage Sources -> Browse) imports a `b:Sources` document and
then cites from its own bibliography manager, which is how the Word writers in the
group get the same service the .bib file gives the LaTeX writers.

`b:Tag` reuses the BibTeX cite key so a paper is referred to the same way on both
sides. Escaping is left to ElementTree; Unicode is kept as-is.
"""

import json
import re
from xml.etree import ElementTree as ET

from app.bibtex import make_cite_key, unique_key
from app.models import Article

NS = "http://schemas.openxmlformats.org/officeDocument/2006/bibliography"

_SOURCE_TYPE_MAP = {
    "journal-article": "JournalArticle",
    "proceedings-article": "ConferenceProceedings",
    "book-chapter": "BookSection",
    "book": "Book",
    "monograph": "Book",
    "edited-book": "Book",
    "report": "Report",
    "dissertation": "Report",
}


def split_name(full_name: str) -> tuple[str, str]:
    """Split a joined name into (last, first-and-middle). Crossref given/family wins
    when available; this is only for stub records that never got metadata."""
    parts = full_name.split()
    if not parts:
        return "", ""
    return parts[-1], " ".join(parts[:-1])


def _pages(raw: str | None) -> str | None:
    """Crossref `page` is '45-67' or '45'. Word renders the range verbatim, so
    normalise the en/em dashes publishers use down to a plain hyphen."""
    if not raw:
        return None
    return re.sub(r"[‐-―]", "-", raw)


def _fields(article: Article) -> tuple[str, dict[str, str | None], list[tuple[str, str]]]:
    """Return (base tag, simple b: fields, [(last, first)]) for one article."""
    if article.crossref_json:
        message = json.loads(article.crossref_json)
        author_entries = message.get("author") or []
        names = [
            (a.get("family") or "", a.get("given") or "")
            if a.get("family")
            else split_name(a.get("name") or "")
            for a in author_entries
        ]
        date_parts = (message.get("issued") or {}).get("date-parts") or [[]]
        year = date_parts[0][0] if date_parts[0] else None
        title = (message.get("title") or [None])[0]
        first_last = author_entries[0].get("family") if author_entries else None
        fields = {
            "SourceType": _SOURCE_TYPE_MAP.get(message.get("type", ""), "Misc"),
            "Title": title,
            "JournalName": (message.get("container-title") or [None])[0],
            "Year": str(year) if year else None,
            "Volume": message.get("volume"),
            "Issue": message.get("issue"),
            "Pages": _pages(message.get("page")),
            "DOI": message.get("DOI"),
            "Publisher": message.get("publisher"),
            "URL": message.get("URL"),
        }
    else:
        names = [split_name(author.full_name) for author in article.authors]
        year = article.year
        title = article.title
        first_last = names[0][0] if names else None
        fields = {
            "SourceType": "Misc",
            "Title": title,
            "JournalName": article.journal,
            "Year": str(year) if year else None,
            "DOI": article.doi,
            "URL": article.source_url,
        }
    return make_cite_key(first_last, year, title), fields, names


def _append_source(
    parent: ET.Element, tag: str, fields: dict[str, str | None], names: list[tuple[str, str]]
) -> None:
    source = ET.SubElement(parent, f"{{{NS}}}Source")
    ET.SubElement(source, f"{{{NS}}}Tag").text = tag
    # SourceType must come first for Word to pick the right entry form.
    ET.SubElement(source, f"{{{NS}}}SourceType").text = fields["SourceType"]
    if names:
        # Word nests the contributor role inside <b:Author>: <b:Author><b:Author><b:NameList>.
        role = ET.SubElement(ET.SubElement(source, f"{{{NS}}}Author"), f"{{{NS}}}Author")
        name_list = ET.SubElement(role, f"{{{NS}}}NameList")
        for last, first in names:
            person = ET.SubElement(name_list, f"{{{NS}}}Person")
            ET.SubElement(person, f"{{{NS}}}Last").text = last
            if first:
                ET.SubElement(person, f"{{{NS}}}First").text = first
    for name, value in fields.items():
        if name == "SourceType" or value in (None, ""):
            continue
        ET.SubElement(source, f"{{{NS}}}{name}").text = str(value)


# A document is a header, the <b:Source> elements, and a footer. The offline export
# page glues the same three pieces together in JavaScript for whatever subset the
# reader ticks, so both sides have to agree on them.
DOC_OPEN = (
    '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
    f'<b:Sources xmlns:b="{NS}" SelectedStyle="">\n'
)
DOC_CLOSE = "\n</b:Sources>"

# ElementTree repeats the namespace declaration on every element it serialises on
# its own; the wrapper above already carries it, so drop it from the fragments.
_NS_DECL = re.compile(r'\s+xmlns:b="[^"]*"')


def export_sources(articles: list[Article]) -> list[tuple[str, str]]:
    """One (tag, serialized <b:Source> element) pair per article, tags deduplicated
    across the whole list exactly as the BibTeX export deduplicates cite keys.

    The fragments declare no namespace of their own — they are meant to be joined
    between `DOC_OPEN` and `DOC_CLOSE`.
    """
    ET.register_namespace("b", NS)
    used: set[str] = set()
    fragments = []
    for article in articles:
        base_tag, fields, names = _fields(article)
        tag = unique_key(base_tag, used)
        holder = ET.Element(f"{{{NS}}}Sources")
        _append_source(holder, tag, fields, names)
        source = holder[0]
        ET.indent(source)  # indent the fragment itself, so it closes at column 0
        text = ET.tostring(source, encoding="unicode").strip()
        fragments.append((tag, _NS_DECL.sub("", text, count=1)))
    return fragments


def export_word_xml(articles: list[Article]) -> str:
    """Render articles as one Word Source Manager document, deduplicating tags with
    a/b/c suffixes exactly as the BibTeX export deduplicates cite keys."""
    ET.register_namespace("b", NS)
    root = ET.Element(f"{{{NS}}}Sources", {"SelectedStyle": ""})
    used: set[str] = set()
    for article in articles:
        base_tag, fields, names = _fields(article)
        _append_source(root, unique_key(base_tag, used), fields, names)
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n' + ET.tostring(
        root, encoding="unicode"
    )


def article_word_xml(article: Article) -> str:
    return export_word_xml([article])


def xml_filename(article: Article) -> str:
    """Human-readable download filename, e.g. smith2020quantum.xml."""
    base_tag, _, _ = _fields(article)
    return f"{base_tag}.xml"
