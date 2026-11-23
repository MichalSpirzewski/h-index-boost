"""Link logged-in users to the author records of their publications.

A directory account and a byline rarely spell a name the same way: AD has
"Spirzewski Michał" (surname first, with diacritics), Crossref has "Michał
Spirzewski", a PDF may have "M. Spirzewski" or "Michal Spirzewski". The match
used here is deliberately simple — same surname, same first initial, both
compared without case or diacritics — and is only ever a *suggestion*: the user
confirms on /me which records are really them (`UserAuthor`).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from institute_auth import DirectoryUser
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import ingest
from app.models import Article, Author, DepartmentMember, User, UserAuthor

# Letters NFKD does not decompose into base letter + accent ("ł" is a letter of
# its own in Unicode), so stripping combining marks alone would leave them.
_FOLD = str.maketrans(
    {"ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "ø": "o", "Ø": "o", "ß": "ss",
     "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe", "ı": "i"}
)
_NOT_NAME = re.compile(r"[^\w\s-]")  # dots of initials, commas, apostrophes


def fold(text: str | None) -> str:
    """'Michał  P.' -> 'michal p'; 'Kowalska-Nowak' keeps its hyphen."""
    decomposed = unicodedata.normalize("NFKD", (text or "").translate(_FOLD))
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(_NOT_NAME.sub(" ", plain.casefold()).split())


def _byline_tokens(full_name: str) -> list[str]:
    """Author name as folded tokens in given…surname order ('Nowak, Jan' reordered)."""
    if "," in full_name:
        family, _, given = full_name.partition(",")
        return fold(given).split() + fold(family).split()
    return fold(full_name).split()


def name_matches(full_name: str, surname: str | None, given_name: str | None) -> bool:
    """True when the byline ends with the surname and starts with the given
    name's initial: 'Michał Spirzewski', 'M. Spirzewski', 'Michal J. Spirzewski'
    all match (Spirzewski, Michał); 'Piotr Spirzewski' and 'Michał Spirzewska' do not.
    """
    surname_tokens = fold(surname).split()
    given = fold(given_name)
    tokens = _byline_tokens(full_name)
    if not surname_tokens or not given or len(tokens) <= len(surname_tokens):
        return False
    return tokens[-len(surname_tokens):] == surname_tokens and tokens[0][0] == given[0]


def name_candidates(user: DirectoryUser) -> list[tuple[str, str]]:
    """(surname, given name) pairs to try for this user.

    The directory's own split when it has one. Otherwise (a login stored before
    givenName/sn were read) the display name is split both ways, since its order
    is a local convention — the user confirms the result anyway.
    """
    if user.surname and user.given_name:
        return [(user.surname, user.given_name)]
    tokens = user.display_name.split()
    if len(tokens) < 2:
        return []
    return [(tokens[0], " ".join(tokens[1:])), (tokens[-1], " ".join(tokens[:-1]))]


def suggested_authors(session: Session, user: DirectoryUser) -> list[Author]:
    """Canonical authors whose name — or a merged variant's — matches the user."""
    candidates = name_candidates(user)
    if not candidates:
        return []
    found: dict[int, Author] = {}
    for author in session.scalars(select(Author)):
        if any(name_matches(author.full_name, s, g) for s, g in candidates):
            canonical = ingest.canonical_author(session, author)
            found[canonical.id] = canonical
    return sorted(found.values(), key=lambda a: (a.full_name.casefold(), a.id))


def confirmed_author_ids(session: Session, user: DirectoryUser | None) -> set[int]:
    """The user's confirmed authors, mapped to canonical ids (a confirmed row may
    have been merged into another since)."""
    if user is None:
        return set()
    ids = set(
        session.scalars(select(UserAuthor.author_id).where(UserAuthor.username == user.username))
    )
    canonical = set()
    for author_id in ids:
        author = session.get(Author, author_id)
        if author is not None:
            canonical.add(ingest.canonical_author(session, author).id)
    return canonical


def set_confirmed_authors(
    session: Session, user: DirectoryUser, selected: Iterable[int], allowed: Iterable[int]
) -> set[int]:
    """Replace the user's confirmed authors with `selected ∩ allowed`.

    `allowed` is what the page offered (suggestions plus already-confirmed), so a
    hand-crafted request cannot claim an arbitrary author.
    """
    keep = set(selected) & set(allowed)
    session.execute(delete(UserAuthor).where(UserAuthor.username == user.username))
    session.add_all(UserAuthor(username=user.username, author_id=a) for a in sorted(keep))
    session.commit()
    return keep


def is_author_of(session: Session, user: DirectoryUser | None, article: Article) -> bool:
    """Whether the user has confirmed one of this paper's author records."""
    confirmed = confirmed_author_ids(session, user)
    if not confirmed:
        return False
    return any(
        ingest.canonical_author(session, link.author).id in confirmed
        for link in article.author_links
    )


def department_authors(session: Session, department: str) -> dict[int, set[str]]:
    """Canonical author id -> usernames of the department members it belongs to.

    Two sources: an author record confirmed on /me by a user of that department
    (exact), and a surname + first-initial match against the department roster
    read from AD (`DepartmentMember`) — which also covers colleagues who never
    log in, but can mistake a namesake for staff. Not used by any page yet; the
    groundwork for distinguishing department papers.
    """
    found: dict[int, set[str]] = {}
    members = list(
        session.scalars(select(DepartmentMember).where(DepartmentMember.department == department))
    )
    if members:
        for author in session.scalars(select(Author)):
            hits = {
                m.username
                for m in members
                if name_matches(author.full_name, m.surname, m.given_name)
            }
            if hits:
                canonical = ingest.canonical_author(session, author)
                found.setdefault(canonical.id, set()).update(hits)

    confirmed = session.execute(
        select(UserAuthor.author_id, User.username)
        .join(User, User.username == UserAuthor.username)
        .where(User.department == department)
    ).all()
    for author_id, username in confirmed:
        author = session.get(Author, author_id)
        if author is not None:
            found.setdefault(ingest.canonical_author(session, author).id, set()).add(username)
    return found
