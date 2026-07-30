"""Metadata extraction for scientific project deliverables and milestones."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime

import fitz


@dataclass
class ParsedProjectDocument:
    project_number: str | None = None
    document_type: str | None = None
    entity_key: str | None = None
    title: str | None = None
    lead_beneficiary: str | None = None
    authors: list[str] = field(default_factory=list)
    published_date: str | None = None
    full_text: str | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


_PROJECT_NUMBER_RE = re.compile(
    r"Project\s+Number\s*[:#]?\s*([A-Z0-9][A-Z0-9._/-]*)",
    re.IGNORECASE,
)
_TYPE_RE = re.compile(
    r"^\s*(MILESTONE|DELIVERABLE)\s+([A-Z]{0,4}\s*\d+(?:\.\d+)*)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_LEAD_RE = re.compile(r"^\s*Lead beneficiary\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_AUTHORS_RE = re.compile(
    r"Author\s*\(s\)\s*(.*?)\s*Final version released on\s*:",
    re.IGNORECASE | re.DOTALL,
)
_RELEASE_DATE_RE = re.compile(
    r"Final version released on\s*:\s*(\d{1,2}/\d{1,2}/\d{4})",
    re.IGNORECASE,
)


def extract_project_document_text(pdf_bytes: bytes) -> str | None:
    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return None
    with document:
        text = "\n".join(page.get_text() for page in document)
    return text.strip() or None


def _one_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _authors(value: str) -> list[str]:
    joined = _one_line(value)
    return [name.strip() for name in re.split(r",\s*", joined) if name.strip()]


def _iso_date(value: str) -> str | None:
    try:
        return datetime.strptime(value, "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def parse_project_document_text(text: str) -> ParsedProjectDocument:
    parsed = ParsedProjectDocument(full_text=text)

    project_match = _PROJECT_NUMBER_RE.search(text)
    if project_match:
        parsed.project_number = project_match.group(1).strip()
    else:
        parsed.warnings.append("Project number was not found.")

    type_matches = list(_TYPE_RE.finditer(text))
    if type_matches:
        # Headers such as "TREASURE - Milestone MS12" are deliberately ignored;
        # prefer the standalone cover-page classification, normally the last hit.
        type_match = type_matches[-1]
        parsed.document_type = type_match.group(1).casefold()
        parsed.entity_key = re.sub(r"\s+", "", type_match.group(2)).upper()

        following = text[type_match.end() :]
        lead_match = _LEAD_RE.search(following)
        if lead_match:
            title_block = following[: lead_match.start()]
            parsed.title = _one_line(title_block) or None
            parsed.lead_beneficiary = _one_line(lead_match.group(1))
    else:
        parsed.warnings.append("Document type and code were not found.")

    authors_match = _AUTHORS_RE.search(text)
    if authors_match:
        parsed.authors = _authors(authors_match.group(1))
    else:
        parsed.warnings.append("Authors were not found.")

    date_match = _RELEASE_DATE_RE.search(text)
    if date_match:
        parsed.published_date = _iso_date(date_match.group(1))
        if parsed.published_date is None:
            parsed.warnings.append("Release date has an unsupported format.")
    else:
        parsed.warnings.append("Final release date was not found.")

    if parsed.title is None:
        parsed.warnings.append("Document title was not found.")
    if parsed.lead_beneficiary is None:
        parsed.warnings.append("Lead beneficiary was not found.")
    return parsed


def parse_project_document_pdf(pdf_bytes: bytes) -> ParsedProjectDocument:
    text = extract_project_document_text(pdf_bytes)
    if text is None:
        return ParsedProjectDocument(warnings=["PDF text could not be extracted."])
    return parse_project_document_text(text)


def encode_authors(authors: list[str]) -> str:
    return json.dumps(authors, ensure_ascii=False)


def decode_authors(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def encode_warnings(warnings: list[str]) -> str:
    return json.dumps(warnings, ensure_ascii=False)


def decode_warnings(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []
