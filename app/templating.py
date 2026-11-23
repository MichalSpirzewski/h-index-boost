"""Jinja environment shared by the served pages and the offline site export.

Lives outside `main.py` so `site_export.py` can render a template without importing
the FastAPI app (which imports it back).
"""

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from institute_auth import TEMPLATES_DIR as AUTH_TEMPLATES_DIR

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _auth_context(request: Request) -> dict:
    """`current_user` in every served page: the logged-in `DirectoryUser` or None.

    Pages rendered without a request (the offline site export) never see it,
    so anything guarded by `{% if current_user %}` is left out of them.
    """
    return {"current_user": getattr(request.state, "user", None)}


templates = Jinja2Templates(
    directory=[TEMPLATES_DIR, AUTH_TEMPLATES_DIR], context_processors=[_auth_context]
)


def short_author_name(full_name: str) -> str:
    """'Michał Spirzewski' -> 'M. Spirzewski'; single-token names unchanged."""
    parts = full_name.split()
    if len(parts) < 2:
        return full_name
    return f"{parts[0][0]}. {parts[-1]}"


templates.env.filters["short_name"] = short_author_name
