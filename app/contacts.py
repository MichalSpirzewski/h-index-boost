"""Auxiliary, hand-curated contact data for NCBJ authors.

Deliberately kept out of the ingest-managed database: this is manually entered,
may hold personal data (phone, meeting link), and lives in a gitignored JSON file
(`data/author_contacts.json`). Keyed by canonical author id.

    { "2": {"name": "…", "phone": "…", "meeting_link": "…"} }

No e-mail field: addresses used to be harvested from PDFs, but the store survives
database rebuilds while author ids do not, so entries silently came to point at
the wrong people. Nothing here is derived from ingest any more — manual only.
"""

from __future__ import annotations

import json
import os
import tempfile

from app import db

FIELDS = ("phone", "meeting_link")


def _path():
    # Resolve from db.DATA_DIR each call so tests pointing at a temp dir are honored.
    return db.DATA_DIR / "author_contacts.json"


def load_all() -> dict[str, dict[str, str]]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def get(author_id: int) -> dict[str, str]:
    return load_all().get(str(author_id), {})


def save(author_id: int, name: str, values: dict[str, str | None]) -> dict[str, str]:
    """Upsert an author's contact entry. Blank fields are cleared; an entry with no
    contact fields left is removed entirely so the file stays tidy."""
    data = load_all()
    key = str(author_id)
    entry: dict[str, str] = {}
    for field in FIELDS:
        value = (values.get(field) or "").strip()
        if value:
            entry[field] = value
    if entry:
        entry["name"] = name
        data[key] = entry
    else:
        data.pop(key, None)
    _atomic_write(data)
    return entry


def _atomic_write(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
