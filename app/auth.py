"""RefBase's use of the institute (Active Directory) login.

The mechanics live in the reusable `institute_auth` package from the separate
ncbj-login repository (git@code.cis.gov.pl:ncbj_apps/ncbj-login.git).
This file holds only what is RefBase policy:

- reading is open to everyone; changing anything needs a login (`needs_login`);
- projects and their documents are visible only to members of the project's AD
  group (`can_see_project`), and not at all to visitors who are not logged in;
- what the directory said at login is kept in the `users` table (`DbUserStore`).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from collections.abc import Iterable
from datetime import UTC, datetime

from fastapi import Request
from institute_auth import DirectorySettings, DirectoryUser, Group, InstituteAuth

from app import db
from app.models import DepartmentMember, Project, User
from app.templating import templates

log = logging.getLogger(__name__)

SESSION_DAYS = 14

# POST routes that only *package* existing data for download. Every other
# non-GET request needs a login, so a newly added write route is protected unless
# someone deliberately lists it here.
_OPEN_POSTS = frozenset(
    {
        "/export/bibtex",
        "/export/word-xml",
        "/export/pdfs",
        "/export/all",
        "/export/site",
    }
)
# GET pages that need a login: the upload form, everything about projects, and
# the user's own page.
_LOGIN_PAGE_PREFIXES = ("/upload", "/projects", "/project-documents", "/me")
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def needs_login(request: Request) -> bool:
    path = request.url.path
    # /api/v1 is for scripts and has its own X-API-Key check (REFBASE_API_KEY).
    if path.startswith(("/static/", "/api/v1/")) or path == "/api/v1":
        return False
    if request.method in _SAFE_METHODS:
        return any(path == p or path.startswith(p + "/") for p in _LOGIN_PAGE_PREFIXES)
    return path not in _OPEN_POSTS


class DbUserStore:
    """`institute_auth.UserStore` backed by the `users` table."""

    def save(self, user: DirectoryUser) -> None:
        groups = json.dumps(
            [{"dn": g.dn, "name": g.name} for g in user.groups], ensure_ascii=False
        )
        now = datetime.now(UTC)
        with db.SessionLocal() as session:
            row = session.get(User, user.username)
            if row is None:
                row = User(username=user.username, first_login_at=now)
                session.add(row)
            row.display_name = user.display_name
            row.email = user.email
            row.department = user.department
            row.dn = user.dn
            row.given_name = user.given_name
            row.surname = user.surname
            row.groups_json = groups
            row.last_login_at = now
            if user.department and user.department_roster is not None:
                _replace_roster(session, user.department, user.department_roster, now)
            session.commit()

    def load(self, username: str) -> DirectoryUser | None:
        with db.SessionLocal() as session:
            row = session.get(User, username)
            if row is None:
                return None
            return DirectoryUser(
                username=row.username,
                display_name=row.display_name,
                email=row.email,
                department=row.department,
                dn=row.dn,
                groups=tuple(Group(dn=g["dn"], name=g["name"]) for g in row.groups),
                given_name=row.given_name,
                surname=row.surname,
            )


def _replace_roster(session, department: str, roster, now: datetime) -> None:
    """Make `department_members` for this department exactly the directory's list.

    People who moved department disappear here and reappear under their new one
    when someone from there logs in. A failed read (`roster is None`) never
    reaches this, so an unreachable directory cannot wipe a stored roster.
    """
    session.query(DepartmentMember).filter(DepartmentMember.department == department).delete()
    for person in roster:
        session.merge(
            DepartmentMember(
                username=person.username,
                department=department,
                display_name=person.display_name,
                given_name=person.given_name,
                surname=person.surname,
                active=person.active,
                refreshed_at=now,
            )
        )


def _session_secret() -> str:
    """REFBASE_SESSION_SECRET, or a random one kept in the data dir.

    The file fallback means a fresh checkout works without setup and logins
    survive restarts; set the variable in production so the secret is managed
    with the rest of the deployment's configuration.
    """
    secret = os.environ.get("REFBASE_SESSION_SECRET", "").strip()
    if secret:
        return secret
    path = db.DATA_DIR / "session_secret"
    if not path.exists():
        path.touch(mode=0o600)
        path.write_text(secrets.token_urlsafe(48))
    return path.read_text().strip()


def _cookie_secure() -> bool:
    return os.environ.get("REFBASE_COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes"}


auth = InstituteAuth(
    settings=DirectorySettings.from_env("REFBASE_LDAP_"),
    secret_key=_session_secret(),
    store=DbUserStore(),
    needs_login=needs_login,
    templates=templates,
    login_template="login.html",
    max_age=SESSION_DAYS * 24 * 3600,
    https_only=_cookie_secure(),
    cookie_name="refbase_session",
    # Read everyone in the user's department at login (stored by DbUserStore):
    # groundwork for telling department papers apart, see people.department_authors.
    with_department_roster=True,
)


# --------------------------------------------------------------------------- projects
def can_see_project(user: DirectoryUser | None, project: Project) -> bool:
    if user is None:
        return False
    if project.ad_group_dn is None:  # pre-login project not yet assigned a group
        return True
    return user.in_group(project.ad_group_dn)


def visible_projects(user: DirectoryUser | None, projects: Iterable[Project]) -> list[Project]:
    return [project for project in projects if can_see_project(user, project)]


def user_group(user: DirectoryUser | None, group_dn: str | None) -> Group | None:
    """The user's own group with this DN — the only groups they may pick for a project."""
    if user is None or not group_dn:
        return None
    wanted = group_dn.casefold()
    return next((g for g in user.groups if g.dn.casefold() == wanted), None)
