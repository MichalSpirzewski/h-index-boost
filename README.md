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
