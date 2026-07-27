#!/usr/bin/env python
"""Re-run the ingestion pipeline over every PDF already sitting in data/pdfs/.

For each PDF this:
  1. resolves the DOI (from the PDF text/metadata, falling back to the filename,
     which is the normalized DOI with '/' -> '_'),
  2. finds or creates the matching Article row,
  3. attaches the on-disk PDF (full text for FTS, abstract backfill, keywords),
  4. fetches metadata (Crossref required; Semantic Scholar / Unpaywall best-effort)
     via ingest.process_article, and marks the row `ready`.

Existing rows are refreshed in place — safe to run repeatedly. Crossref JSON that's
already stored is never re-fetched (see process_article). Be polite: sequential,
single worker, mailto set.

Usage:
  scripts/reingest_pdfs.py [--force-refetch] [PDF ...]

  --force-refetch   clear stored crossref_json first so metadata is pulled fresh
  PDF ...           limit to specific files (defaults to every *.pdf in data/pdfs/)
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `import app...` work when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app import db, ingest  # noqa: E402
from app.models import Article  # noqa: E402


def _doi_from_filename(pdf_path: Path) -> str | None:
    """data/pdfs/10.1016_j.net.2026.104418.pdf -> 10.1016/j.net.2026.104418.

    save_pdf() encodes the (single) DOI slash as '_', so restore the first one.
    """
    stem = pdf_path.stem
    return stem.replace("_", "/", 1) if stem.startswith("10.") else None


def reingest(pdf_path: Path, force_refetch: bool) -> None:
    pdf_bytes = pdf_path.read_bytes()
    doi = ingest.extract_doi_from_pdf(pdf_bytes) or _doi_from_filename(pdf_path)
    if not doi:
        print(f"  SKIP  {pdf_path.name}: no DOI found in PDF or filename")
        return

    with db.SessionLocal() as session:
        article = session.scalar(select(Article).where(Article.doi == doi))
        created = article is None
        if created:
            article = Article(doi=doi, status="pending")
            session.add(article)
            session.commit()

        if force_refetch:
            article.crossref_json = None
        # Point the row at this exact file and (re)build text/abstract/keywords.
        ingest.attach_pdf(session, article, pdf_bytes)
        article_id = article.id

    # Crossref / S2 / Unpaywall + status transition; own session, safe to repeat.
    ingest.process_article(article_id)

    with db.SessionLocal() as session:
        article = session.get(Article, article_id)
        tag = "NEW " if created else "UPD "
        print(
            f"  {tag} [{article.id}] {doi}  status={article.status}  "
            f"title={(article.title or '(none)')[:60]!r}"
        )


def main() -> int:
    args = sys.argv[1:]
    force_refetch = "--force-refetch" in args
    files = [a for a in args if a != "--force-refetch"]

    if files:
        pdfs = [Path(f) for f in files]
    else:
        pdfs = sorted(db.PDF_DIR.glob("*.pdf"))

    if not pdfs:
        print(f"No PDFs found in {db.PDF_DIR}")
        return 1

    db.init_db()
    print(f"Reingesting {len(pdfs)} PDF(s) from {db.PDF_DIR}"
          + (" [force-refetch]" if force_refetch else ""))
    for pdf in pdfs:
        reingest(pdf, force_refetch)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
