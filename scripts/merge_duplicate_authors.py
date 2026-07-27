#!/usr/bin/env python
"""Group duplicate authors under a single canonical ("base") author.

Two ways duplicates arise: the same name ingested with and without an ORCID, or
slightly different name strings for one person. This script:

  * auto-merges exact-name groups (case/whitespace-insensitive) into the row that
    has an ORCID, else the lowest id — safe, since v1 treats an exact name as one
    person anyway;
  * prints fuzzy near-name pairs (rapidfuzz) for you to confirm by hand.

Merging is a soft operation (see ingest.merge_authors): article links move to the
canonical author and the duplicate row is kept with `merged_into_id` set.

Usage:
  scripts/merge_duplicate_authors.py                 # auto-merge exact names, suggest fuzzy
  scripts/merge_duplicate_authors.py --merge SRC DST # manually fold author SRC into DST
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rapidfuzz import fuzz  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app import db, ingest  # noqa: E402
from app.models import Article, ArticleAuthor, Author  # noqa: E402

FUZZY_THRESHOLD = 80  # token_sort_ratio; below this we don't even suggest


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().casefold())


def _pub_count(session, author_id: int) -> int:
    return session.scalar(
        select(func.count(ArticleAuthor.article_id))
        .join(Article, Article.id == ArticleAuthor.article_id)
        .where(ArticleAuthor.author_id == author_id, Article.hidden.is_(False))
    ) or 0


def _pick_canonical(session, authors: list[Author]) -> Author:
    """Prefer a row with an ORCID; break ties by most publications, then lowest id."""
    return sorted(
        authors,
        key=lambda a: (a.orcid is None, -_pub_count(session, a.id), a.id),
    )[0]


def auto_merge_exact() -> None:
    with db.SessionLocal() as session:
        active = session.scalars(
            select(Author).where(Author.merged_into_id.is_(None))
        ).all()

        groups: dict[str, list[Author]] = defaultdict(list)
        for author in active:
            groups[_norm(author.full_name)].append(author)

        merged_any = False
        for _, members in groups.items():
            if len(members) < 2:
                continue
            target = _pick_canonical(session, members)
            for source in members:
                if source.id == target.id:
                    continue
                moved = ingest.merge_authors(session, source, target)
                print(
                    f"  merged [{source.id}] {source.full_name!r} "
                    f"-> [{target.id}] {target.full_name!r}  ({moved} link(s) moved)"
                )
                merged_any = True
        session.commit()
        if not merged_any:
            print("  (no exact-name duplicates found)")


def suggest_fuzzy() -> None:
    with db.SessionLocal() as session:
        active = session.scalars(
            select(Author).where(Author.merged_into_id.is_(None)).order_by(Author.id)
        ).all()
        shown = False
        for i, a in enumerate(active):
            for b in active[i + 1:]:
                if _norm(a.full_name) == _norm(b.full_name):
                    continue  # already handled by exact pass
                score = fuzz.token_sort_ratio(a.full_name.lower(), b.full_name.lower())
                subset = fuzz.token_set_ratio(a.full_name.lower(), b.full_name.lower())
                if score >= FUZZY_THRESHOLD or subset >= 95:
                    shown = True
                    print(
                        f"  ? [{a.id}] {a.full_name!r}  ~  [{b.id}] {b.full_name!r}  "
                        f"(sort={score:.0f}, set={subset:.0f})"
                    )
                    print(
                        f"      if same person: "
                        f"scripts/merge_duplicate_authors.py --merge {a.id} {b.id}"
                    )
        if not shown:
            print("  (no fuzzy near-matches above threshold)")


def manual_merge(source_id: int, target_id: int) -> None:
    with db.SessionLocal() as session:
        source = session.get(Author, source_id)
        target = session.get(Author, target_id)
        if source is None or target is None:
            print("error: source or target author id not found", file=sys.stderr)
            raise SystemExit(1)
        moved = ingest.merge_authors(session, source, target)
        session.commit()
        canonical = ingest.canonical_author(session, target)
        print(
            f"merged [{source_id}] {source.full_name!r} -> "
            f"[{canonical.id}] {canonical.full_name!r}  ({moved} link(s) moved)"
        )


def main() -> int:
    db.init_db()
    args = sys.argv[1:]
    if args and args[0] == "--merge":
        if len(args) != 3:
            print("usage: --merge SOURCE_ID TARGET_ID", file=sys.stderr)
            return 1
        manual_merge(int(args[1]), int(args[2]))
        return 0

    print("Auto-merging exact-name duplicates:")
    auto_merge_exact()
    print("\nFuzzy near-name suggestions (review manually):")
    suggest_fuzzy()
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
