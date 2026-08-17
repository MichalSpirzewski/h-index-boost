# RefBase

Self-hosted, no-login shared reference library for a research group. See [CLAUDE.md](CLAUDE.md) for full project scope and conventions.

## Publication objects

The original `Article` model is now exposed as a general `Publication` base
class with `JournalPublication` and `ConferencePublication` subtypes. The
hierarchy uses a `publication_type` discriminator in the existing `articles`
table, so existing databases and URLs remain compatible. `Article` remains an
import alias for `Publication`; new code should prefer the explicit names.

Journal publications use the existing journal and publication-date metadata.
Conference publications additionally support conference name, proceedings
title, location, and start/end dates.

## Keywords and topics

Keywords are what the sources give: Crossref subjects, Semantic Scholar fields of
study, and the "Keywords:" line of an uploaded PDF. They are stored verbatim and
never merged, which makes them precise but too exclusive to browse — a small
library easily carries more keywords than papers.

Topics are the curated layer above them. Someone creates a topic at `/topics`,
then clicks the keywords that belong to it; the page offers every keyword in the
library, and one click classifies it. A paper belongs to a topic as long as one
of its keywords does, so reclassifying a keyword moves every paper carrying it at
once. A keyword may sit in several topics. Deleting a topic removes the grouping
only — keywords and papers are untouched.

The dashboard shows a Topics panel and a Keywords panel; `/?topic={id}` and
`/?keyword={id}` filter the publication tables.

Databases created before this split stored keywords in a table called `topics`;
`init_db()` renames it (and `article_topics`) to `keywords`/`article_keywords` on
startup and leaves `topics` to the new grouping model. The migration is
idempotent and also recovers a database where the new tables were created empty
before it landed.

## Project-document objects

`ProjectDocument` is the base for non-publication files associated with a
scientific project. Its initial subtypes are `DeliverableDocument` and
`MilestoneDocument`. They keep the external project ID and stable entity code
(for example `D2.1` or `MS3`) that will connect them to the project-management
service.

The browser UI provides local project workspaces at `/projects`. A project page
keeps deliverables and milestones in separate lists and accepts project files
through a click-to-select or drag-and-drop field. PDF, Word, Excel, PowerPoint,
and text files up to 50 MB are stored separately from publication PDFs under
`data/project_documents/`. Each logical document currently has one file;
version-history objects will be added separately.

Every local project has a unique project number. Uploaded PDFs are parsed for
their project number, deliverable/milestone type and code, title, lead
beneficiary, authors, and final-release date. When the parsed project number
belongs to a different existing workspace, the document is automatically
attributed to that project. Parsed PDF text is retained for future full-text
search; incomplete extraction is surfaced as parsing notes on the project page.

## Duplicates and soft delete

A paper's DOI is what makes it unique, so a paper that has none — no DOI in the
PDF, none registered — could be uploaded any number of times and produce a new
record each time. Every stored PDF therefore carries a `pdf_sha256` of its bytes,
and an upload whose file is already in the library takes the same "already in
library, here it is" path a duplicate DOI takes: HTTP 200 and a link, never an
error. Notes:

- DOI resolution runs first, so handing a DOI to a file already stored still
  enriches that record instead of being turned away.
- The lookup ignores hidden records — their links 404, and re-adding a paper
  someone removed is a legitimate thing to do.
- It catches byte-identical files, which is the common accident (one file, three
  uploads). A fresh download of the same article from the publisher can differ in
  its trailer and will still get through; the fuzzy title warning is the backstop
  there.
- The column is indexed but deliberately **not** unique: copies already in a
  library share a hash, as do hidden ones.

`POST /articles/{id}/hide` removes a record from every listing — dashboard,
tables, search, author, journal and topic pages, exports — and `/articles/{id}`
then 404s. Nothing is deleted: the PDF, metadata and keyword links stay, so
`GET /hidden` can list what has been put away and restore any of it in one click
via `POST /articles/{id}/unhide`. The dashboard shows a `Hidden (n)` link only
while something is in there.

## Reading a paper's front page

A PDF with no DOI is the only source of its own metadata, and TeX-produced journal
articles leave the PDF metadata title empty — such uploads used to land with no
title and nobody credited. The parser therefore reads page 1's *layout* rather
than its flat text:

- **Title** — the first run of lines set in the largest face on the page, however
  many lines it wraps onto, skipping mastheads ("Open Access Journal", homepage
  URLs) that are sometimes set just as large.
- **Authors** — the byline directly under the title, with superscript spans
  dropped. This matters because flat text extraction fuses an affiliation marker
  onto the surname in front of it: `Maciej Skrzypek` + superscript `a` comes back
  as `Maciej Skrzypeka`. The layout keeps the marker in its own, smaller,
  superscript-flagged span, so the names come out clean.
- **Journal** — the running head above the title, cut at the citation apparatus
  that identifies it as one: `Journal of Power Technologies 94 (Nuclear Issue)
  (2014) 41–50` → `Journal of Power Technologies`, `Energies 2023, 16, 4567` →
  `Energies`. A masthead line carrying no volume/year/pages is left alone rather
  than guessed at, and mastheads that are noise (homepage URLs, `Open Access`,
  `Received …`, copyright lines) are skipped.
- **Keywords** — a `Keywords:` line that ends in a separator is treated as broken
  by the column width and continues onto the line below.

Authors are only created from the PDF for a record with no DOI and no stored
Crossref message; where Crossref has a byline it owns it, including the order.
The same holds for the title and journal: the PDF only fills what is still empty.
All of it is backfilled by the "Rescan stored PDF" button, so records ingested
before this existed can be filled in without re-uploading.

Not parsed from the running head yet: the volume, issue, page range and year that
sit beside the journal name. A record with no year still exports as `@misc` with
an `nd` cite key.

## Replacing a publication's PDF

The article page accepts a new PDF for a record that already has one (or a first
PDF for one that never got any) and re-parses it: full text for search, author
keywords, the abstract, and per-author affiliations. A DOI-less record also
adopts a DOI printed in the new file and then fetches its Crossref metadata; if
another record already holds that DOI, the page says so and the record is left
without one rather than stealing it.

Re-parsing only rewrites what the *replaced* PDF contributed. The outgoing file
is parsed once more before it is dropped, and an abstract or affiliation is
refreshed only where the stored value matches what that file yielded — anything
Crossref supplied survives untouched. Keywords are only ever added, never
removed, because they are curated into topics by hand.

Uploads are checked for the `%PDF` signature and a 50 MB ceiling before anything
is written, so a mis-picked file cannot overwrite a good one.

## Metadata API

Versioned JSON endpoints are available under `/api/v1`:

- `POST/GET /api/v1/publications`
- `GET/PATCH /api/v1/publications/{id}`
- `POST /api/v1/publications/{id}/archive`
- `POST /api/v1/project-documents`
- `GET/PATCH /api/v1/project-documents/{id}`
- `POST /api/v1/project-documents/{id}/archive`
- `GET /api/v1/projects/{external_project_id}/documents`

FastAPI publishes the interactive OpenAPI documentation at `/docs`. The API is
open for local development when `REFBASE_API_KEY` is unset. When it is set,
clients must send the same value in the `X-API-Key` header.

## Running the app

```bash
./scripts/run.sh
```

This detects which machine you're on, activates the matching conda environment, and starts the app with `uvicorn`. On this dev machine it serves at [http://127.0.0.1:8000](http://127.0.0.1:8000) with `--reload`.

Extra args are passed through to uvicorn, e.g. `./scripts/run.sh --port 9000`.

### First-time environment setup

The script expects a conda env named `refbase` with the project dependencies installed:

```bash
source ~/miniforge3/bin/activate
conda create -y -n refbase python=3.11
conda activate refbase
pip install -r requirements.txt -r requirements-dev.txt
```

## Running tests

```bash
source ~/miniforge3/bin/activate refbase
pytest
```
