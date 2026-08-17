# RefBase — Shared Research Citation Library

Project brief for Claude Code. This file is the source of truth for scope, stack, and conventions. Read it fully before making changes.

## What this app is

A self-hosted, no-login reference library for a research group (~30–40 people). Users add scientific papers (PDF upload and/or DOI/publisher link). The app extracts/fetches metadata via public APIs, stores everything in a shared database, and presents a browsable dashboard of articles, authors, and topics. Any article's PDF and BibTeX can be downloaded; multiple articles can be exported as one merged `.bib` file, or as one XML file importable into Microsoft Word's built-in bibliography manager (the group has both LaTeX and Word writers).

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

Keyword
  id, name (UNIQUE)   -- from Crossref `subject` / S2 `fieldsOfStudy` / PDF keywords

Topic
  id, name (UNIQUE), description (nullable), created_at
  -- a manually curated *group of keywords*, not a tag on a paper

ArticleAuthor
  article_id, author_id, position (int)   -- author order matters for citations

ArticleKeyword
  article_id, keyword_id

TopicKeyword
  topic_id, keyword_id   -- a keyword may belong to several topics

articles_fts (FTS5)
  title, abstract, journal — kept in sync via triggers or on write
```

Keywords vs topics: keywords are whatever the sources supplied and are never merged
or rewritten — that keeps them faithful but far too exclusive to browse (100+
keywords for ~30 papers). Topics are the coarser layer maintained by hand: a topic
holds keywords, and a paper is in a topic as long as one of its keywords is, so
`Publication.topics` is derived from `Publication.keywords` rather than stored.
Never link a paper to a topic directly.

DOI normalization: lowercase, strip `https://doi.org/`, `http://dx.doi.org/`, `doi:` prefixes, trim whitespace. Apply everywhere a DOI enters the system.

Author disambiguation: match on ORCID when present; otherwise exact normalized name match. Do NOT attempt clever fuzzy author merging in v1 — just leave duplicates and add a manual merge admin action later.

## Ingestion pipeline

Endpoint accepts any combination of: PDF file, URL, raw DOI.

1. Resolve DOI: from the raw input → from the URL (regex `10.\d{4,9}/[^\s"<>]+`) → from the PDF text (first 2 pages, then metadata dict).
2. Normalize DOI; check UNIQUE constraint. If duplicate → HTTP 200 with "already in library" + link to existing article. Never a hard error. If a PDF was uploaded and the existing record lacks one, attach it.
2b. No DOI resolved anywhere but a PDF was uploaded → look the file up by `pdf_sha256` (SHA-256 of the bytes, stored on every PDF, indexed but *not* UNIQUE) and take the same "already in library" path. Without this a paper carrying no DOI has nothing to be unique on, and the same upload creates a new record every time. Hidden records are excluded from the lookup: their links 404, and re-adding something soft-deleted is legitimate. DOI resolution runs first, so supplying a DOI for a file already stored still enriches rather than short-circuits.
3. No DOI found → create a stub Article from PDF metadata/user input; run `rapidfuzz` title similarity (threshold ~92) against existing titles and surface a soft warning in the response. Title, journal and authors also come from the front page's *typography* (PyMuPDF's layout dict, not the flat text): the title is the largest-face run of lines on page 1, the running head above it gives the journal (cut at the volume/year/page apparatus), and the byline below it yields authors once the superscript affiliation markers are dropped — flat extraction fuses those onto surnames ("Maciej Skrzypek" + `a` → "Maciej Skrzypeka"). The PDF only ever fills fields that are still empty. Authors are additionally only created for a record with no DOI and no stored Crossref message; where Crossref has one it owns the byline and its order.
4. Return immediately; continue in a `BackgroundTasks` job:
   - Crossref: `GET https://api.crossref.org/works/{doi}` with a `mailto=` param (polite pool). Store full JSON. Extract title, year, journal, abstract, authors (+ ORCID, order), subjects.
   - Semantic Scholar: fieldsOfStudy, abstract fallback. Unpaywall: OA PDF link if no PDF uploaded. Both optional — failures must not fail ingestion.
   - Never re-fetch a DOI whose `crossref_json` is already stored.
5. Save PDF to `data/pdfs/`. Article row gets a `status` (`pending` / `ready` / `metadata_failed`) so the UI can show processing state; HTMX polling on the article row until ready.

Rate limiting: be polite to Crossref (single worker, sequential requests, mailto set). No parallel API hammering.

## Pages / routes

- `GET /` — dashboard: top authors (article count), topic and keyword chips, meeting links, a "Recent additions" panel below them listing the five newest-added papers (library-wide, so filters never narrow it), search box (FTS5). `?keyword={id}` filters to one keyword, `?topic={id}` to every keyword in a topic.
- `GET /articles` — paginated table (50/page), filters: topic, keyword, author, year, full-text query. Row actions: view, download PDF, download BibTeX, select-checkbox.
- `POST /export/bibtex` — selected article ids → single merged `.bib` download. Also `GET /articles/{id}/bibtex` for one.
- `POST /export/word-xml` — same selection → single `.xml` for Microsoft Word's Source Manager. Also `GET /articles/{id}/word-xml` for one.
- `POST /export/site` — same selection → a ZIP holding a self-contained `summary.html` (a copy of the dashboard limited to the selection) and the available selected PDFs, all at the ZIP root. Built in `site_export.py`. CSS and JavaScript are embedded in the HTML, which supports offline sorting, filtering, and expandable paper details. BibTeX is shown in a foldable detail section; no standalone `.bib` or `.xml` is included.
- `POST /shares` — persist the ordered selected article ids under an opaque bearer token; browser forms redirect to the new page and JSON clients receive its absolute URL.
- `GET /shares/{token}` — a live RefBase-hosted version of the selected-publications summary. It shows only currently visible papers from that selection and supports copying the persistent link, sorting, filtering, expandable details, server PDF links, and foldable BibTeX. Anyone with the unguessable link can view it; there is no separate share authentication.
- `GET /articles/{id}` — detail: metadata, authors, keywords, the topics they roll up into, abstract, PDF download, "added by".
- `GET /authors/{id}` — author's articles, co-authors, keywords (`?keyword={id}` filters the table).
- `GET /topics` + `POST /topics` — the topic overview (each topic's paper/keyword counts, plus the keywords in no topic yet) and topic creation. Names are UNIQUE; a clash is a 409 re-render, not a crash.
- `GET /topics/{id}` — one topic: its papers, and a picker holding *every* keyword in the library. `POST /topics/{id}/keywords` with `keyword_id` toggles one keyword in or out — that single click is the whole classification UI. `POST /topics/{id}` renames/describes it, `POST /topics/{id}/delete` drops the grouping (keywords and papers survive).
- `GET /upload` + `POST /api/ingest` — the hybrid upload form (file and/or link/DOI, optional "your name" remembered in localStorage and sent as `added_by`).
- `POST /articles/{id}/replace-pdf` — upload a new PDF for an existing record (or a first one) and re-parse it: full text, keywords, abstract, affiliations, and — for a record without one — a DOI printed in the file, which then triggers the Crossref fetch. Only values the *replaced* PDF supplied are rewritten; Crossref-sourced metadata survives and keywords are never dropped.
- `POST /articles/{id}/hide` / `POST /articles/{id}/unhide` / `GET /hidden` — soft delete, restore, and the list of what has been put away. No hard deletes anywhere, so hiding keeps the PDF, metadata and keyword links intact. The dashboard links to `/hidden` only while something is in it.

BibTeX generation: cite key = `firstauthorlastnameYEARfirstword` (deduplicate with `a`,`b` suffixes within an export). Escape special characters; keep Unicode (biblatex-friendly) but escape `{`, `}`, `%`, `&`, `#`.

Word export: many people in the group write in Microsoft Word, not LaTeX. Word has a built-in bibliography manager (References → Manage Sources) that imports an XML file in the `b:Sources` schema, so the same selection that produces a `.bib` also produces a `.xml` the user imports once and then cites natively from Word. Generate it from the same stored Crossref JSON, in `word_xml.py`, mirroring `bibtex.py`'s structure. Notes:

- Namespace `http://schemas.openxmlformats.org/officeDocument/2006/bibliography`, root `<b:Sources>`, one `<b:Source>` per article.
- `b:Tag` = the BibTeX cite key (reuse `make_cite_key`, same a/b/c dedup) so citations stay consistent between Word and LaTeX users.
- `b:SourceType` mapped from Crossref `type`: `JournalArticle`, `ConferenceProceedings`, `BookSection`, `Book`, `Report`, otherwise `Misc`.
- Authors go in `<b:Author><b:Author><b:NameList>` with `b:Last`/`b:First` per person — split from Crossref `family`/`given`, never from a joined string.
- Use `xml.etree.ElementTree` from the stdlib; it handles XML escaping, so no hand-rolled escaping. Keep Unicode as-is, UTF-8 declaration.
- No Word add-in / "cite while you write" plugin in v1 — that would be a separate Office.js or VBA project outside this stack. File import only.

## Conventions for Claude Code

- Project layout:
  ```
  app/
    main.py            # FastAPI app, routes
    models.py          # SQLAlchemy models
    db.py              # engine, session, WAL pragma, FTS setup
    ingest.py          # pipeline (DOI extraction, Crossref, S2, Unpaywall)
    bibtex.py          # JSON → BibTeX
    word_xml.py        # JSON → Word Source Manager XML
    site_export.py     # selection → self-contained offline page + PDFs (ZIP)
    templating.py      # Jinja env shared by main.py and site_export.py
    affiliations.py    # affiliation normalisation / NCNR grouping
    templates/         # Jinja2 + HTMX partials
    static/
  data/                # sqlite file + pdfs/ (gitignored)
  tests/
  ```
- Type hints everywhere; `ruff` for lint/format; `pytest` for tests.
- Test priorities: DOI normalization/extraction (regex edge cases), dedup behavior, BibTeX escaping and cite-key dedup, JSON→BibTeX field mapping, JSON→Word XML field mapping (author name splitting, source-type mapping, well-formed output), topic grouping (a paper counts once per topic however many of its keywords match) and the legacy topics→keywords table migration. Mock all external APIs in tests (use stored sample Crossref JSON fixtures).
- Keep external API calls isolated in `ingest.py` behind small functions so they're mockable.
- No auth, no user table, no sessions. `added_by` is a plain string.
- Don't add dependencies beyond the stack table without asking.

## Milestones

1. **M1 — Core ingest + storage:** models, DB with WAL+FTS, `/api/ingest` full pipeline, dedup, tests for DOI handling.
2. **M2 — Library UI:** articles list with filters/pagination/search, article detail, PDF download, upload page with processing state.
3. **M3 — Dashboard & exports:** home dashboard, author/topic pages, single + merged BibTeX export, single + merged Word Source Manager XML export (same selection, second download button).
4. **M4 — Polish:** soft delete, "added by" localStorage, duplicate-title warnings, Unpaywall OA fetch, basic error pages.

Start with M1. After each milestone, run tests and show a short summary of what changed before moving on.
