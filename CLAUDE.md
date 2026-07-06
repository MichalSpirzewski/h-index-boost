# RefBase — Shared Research Citation Library

Project brief for Claude Code. This file is the source of truth for scope, stack, and conventions. Read it fully before making changes.

## What this app is

A self-hosted, no-login reference library for a research group (~30–40 people). Users add scientific papers (PDF upload and/or DOI/publisher link). The app extracts/fetches metadata via public APIs, stores everything in a shared database, and presents a browsable dashboard of articles, authors, and topics. Any article's PDF and BibTeX can be downloaded; multiple articles can be exported as one merged `.bib` file.

Think: minimal self-hosted Zotero for a group, without accounts.

## Environment

- Runs on WSL (Ubuntu) during development; will deploy to a small Linux VPS later.
- Python 3.11+, managed with `venv` (or `uv` if available).
- Single-process deployment target: `uvicorn` behind nginx/Caddy. No Docker required for v1 (fine to add later).

## Tech stack (do not substitute without asking)

| Layer | Choice | Notes |
|---|---|---|
| Backend | FastAPI | async endpoints; `BackgroundTasks` for ingestion — no Celery/Redis |
| ORM / DB | SQLAlchemy 2.x + SQLite | enable WAL: `PRAGMA journal_mode=WAL`; FTS5 virtual table for search |
| PDF parsing | PyMuPDF (`fitz`) | text + metadata extraction; DOI regex on first pages |
| Metadata APIs | Crossref (primary), Semantic Scholar (enrichment), Unpaywall (OA links) | plain `requests`/`httpx`; no scraping in v1 |
| BibTeX | generate from stored JSON at export time | `bibtexparser` only if needed; templating is fine |
| Frontend | Server-rendered Jinja2 templates + HTMX | no JS build step; small vanilla JS where unavoidable |
| Fuzzy matching | `rapidfuzz` | soft duplicate warning for DOI-less papers |
| File storage | local disk `data/pdfs/` | filename = normalized DOI (slashes → `_`) or UUID if no DOI |

Rationale for HTMX over React: stateless server, ~40 users, read-heavy dashboard, and the maintainers are researchers, not frontend devs. Keep it boring.

## Data model

All many-to-many via junction tables. Store canonical Crossref JSON; derive BibTeX from it (JSON → BibTeX is lossless enough; the reverse is not).

```
Article
  id, doi (UNIQUE, nullable, normalized), title, year, journal,
  abstract, crossref_json (TEXT), pdf_path (nullable),
  added_by (free text, nullable), source_url (nullable),
  created_at, hidden (bool, default false)   -- soft delete only

Author
  id, full_name, orcid (nullable, UNIQUE when present)

Topic
  id, name (UNIQUE)   -- from Crossref `subject` / S2 `fieldsOfStudy` + manual tags

ArticleAuthor
  article_id, author_id, position (int)   -- author order matters for citations

ArticleTopic
  article_id, topic_id

articles_fts (FTS5)
  title, abstract, journal — kept in sync via triggers or on write
```

DOI normalization: lowercase, strip `https://doi.org/`, `http://dx.doi.org/`, `doi:` prefixes, trim whitespace. Apply everywhere a DOI enters the system.

Author disambiguation: match on ORCID when present; otherwise exact normalized name match. Do NOT attempt clever fuzzy author merging in v1 — just leave duplicates and add a manual merge admin action later.

## Ingestion pipeline

Endpoint accepts any combination of: PDF file, URL, raw DOI.

1. Resolve DOI: from the raw input → from the URL (regex `10.\d{4,9}/[^\s"<>]+`) → from the PDF text (first 2 pages, then metadata dict).
2. Normalize DOI; check UNIQUE constraint. If duplicate → HTTP 200 with "already in library" + link to existing article. Never a hard error. If a PDF was uploaded and the existing record lacks one, attach it.
3. No DOI found → create a stub Article from PDF metadata/user input; run `rapidfuzz` title similarity (threshold ~92) against existing titles and surface a soft warning in the response.
4. Return immediately; continue in a `BackgroundTasks` job:
   - Crossref: `GET https://api.crossref.org/works/{doi}` with a `mailto=` param (polite pool). Store full JSON. Extract title, year, journal, abstract, authors (+ ORCID, order), subjects.
   - Semantic Scholar: fieldsOfStudy, abstract fallback. Unpaywall: OA PDF link if no PDF uploaded. Both optional — failures must not fail ingestion.
   - Never re-fetch a DOI whose `crossref_json` is already stored.
5. Save PDF to `data/pdfs/`. Article row gets a `status` (`pending` / `ready` / `metadata_failed`) so the UI can show processing state; HTMX polling on the article row until ready.

Rate limiting: be polite to Crossref (single worker, sequential requests, mailto set). No parallel API hammering.

## Pages / routes

- `GET /` — dashboard: recent additions, top authors (article count), topic chips, search box (FTS5).
- `GET /articles` — paginated table (50/page), filters: topic, author, year, full-text query. Row actions: view, download PDF, download BibTeX, select-checkbox.
- `POST /export/bibtex` — selected article ids → single merged `.bib` download. Also `GET /articles/{id}/bibtex` for one.
- `GET /articles/{id}` — detail: metadata, authors, topics, abstract, PDF download, "added by".
- `GET /authors/{id}` — author's articles, co-authors, topics.
- `GET /topics/{id}` — articles + most active authors in the topic.
- `GET /upload` + `POST /api/ingest` — the hybrid upload form (file and/or link/DOI, optional "your name" remembered in localStorage and sent as `added_by`).
- `POST /articles/{id}/hide` — soft delete. No hard deletes anywhere.

BibTeX generation: cite key = `firstauthorlastnameYEARfirstword` (deduplicate with `a`,`b` suffixes within an export). Escape special characters; keep Unicode (biblatex-friendly) but escape `{`, `}`, `%`, `&`, `#`.

## Conventions for Claude Code

- Project layout:
  ```
  app/
    main.py            # FastAPI app, routes
    models.py          # SQLAlchemy models
    db.py              # engine, session, WAL pragma, FTS setup
    ingest.py          # pipeline (DOI extraction, Crossref, S2, Unpaywall)
    bibtex.py          # JSON → BibTeX
    templates/         # Jinja2 + HTMX partials
    static/
  data/                # sqlite file + pdfs/ (gitignored)
  tests/
  ```
- Type hints everywhere; `ruff` for lint/format; `pytest` for tests.
- Test priorities: DOI normalization/extraction (regex edge cases), dedup behavior, BibTeX escaping and cite-key dedup, JSON→BibTeX field mapping. Mock all external APIs in tests (use stored sample Crossref JSON fixtures).
- Keep external API calls isolated in `ingest.py` behind small functions so they're mockable.
- No auth, no user table, no sessions. `added_by` is a plain string.
- Don't add dependencies beyond the stack table without asking.

## Milestones

1. **M1 — Core ingest + storage:** models, DB with WAL+FTS, `/api/ingest` full pipeline, dedup, tests for DOI handling.
2. **M2 — Library UI:** articles list with filters/pagination/search, article detail, PDF download, upload page with processing state.
3. **M3 — Dashboard & exports:** home dashboard, author/topic pages, single + merged BibTeX export.
4. **M4 — Polish:** soft delete, "added by" localStorage, duplicate-title warnings, Unpaywall OA fetch, basic error pages.

Start with M1. After each milestone, run tests and show a short summary of what changed before moving on.
