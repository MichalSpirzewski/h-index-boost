"""Jinja environment shared by the served pages and the offline site export.

Lives outside `main.py` so `site_export.py` can render a template without importing
the FastAPI app (which imports it back).
"""

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=TEMPLATES_DIR)


def short_author_name(full_name: str) -> str:
    """'Michał Spirzewski' -> 'M. Spirzewski'; single-token names unchanged."""
    parts = full_name.split()
    if len(parts) < 2:
        return full_name
    return f"{parts[0][0]}. {parts[-1]}"


templates.env.filters["short_name"] = short_author_name
