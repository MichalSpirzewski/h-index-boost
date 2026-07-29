"""Affiliation normalisation, shared by the author pages and the offline export."""

import re
import unicodedata

# Any affiliation mentioning NCNR is folded into this single canonical entry,
# regardless of the street address / spelling variants across PDFs.
NCNR_PHRASE = "national centre for nuclear research"
NCNR_LABEL = "National Centre for Nuclear Research"


def affiliation_key(text: str) -> str:
    """Normalize an affiliation for de-dup: strip case, diacritics, and punctuation."""
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", ascii_only.lower()).strip()


def canonical_affiliation(text: str) -> tuple[str, str]:
    """Return a (dedup-key, display-text) pair, grouping NCNR variants into one."""
    key = affiliation_key(text)
    if NCNR_PHRASE in key:
        return NCNR_PHRASE, NCNR_LABEL
    return key, text
