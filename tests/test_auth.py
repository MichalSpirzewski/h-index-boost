"""Login policy: reading is open, changing needs a login, projects follow AD groups."""

import pytest
from auth_helpers import OTHER_GROUP_DN, TEST_GROUP_DN, TEST_PASSWORD, TEST_USER
from sqlalchemy import select

DOI = "10.1234/auth.test"


def _add_paper(client) -> int:
    response = client.post("/api/ingest", data={"doi": DOI})
    assert response.status_code in (200, 201), response.text
    from app import db
    from app.models import Article

    with db.SessionLocal() as session:
        return session.scalar(select(Article.id).where(Article.doi == DOI))


def _make_project(name: str, group_dn: str | None, number: str) -> str:
    from app import db
    from app.models import Project

    with db.SessionLocal() as session:
        project = Project(
            name=name,
            project_number=number,
            ad_group_dn=group_dn,
            ad_group_name=group_dn and group_dn.split(",")[0][3:],
        )
        session.add(project)
        session.commit()
        return project.id


# --------------------------------------------------------------------------- reading
def test_visitor_reads_library_without_edit_controls(client, anon_client):
    article_id = _add_paper(client)

    home = anon_client.get("/")
    assert home.status_code == 200
    assert 'class="login-button"' in home.text
    assert 'href="/upload"' not in home.text.split("</nav>")[0]
    assert 'href="/projects"' not in home.text
    assert 'formaction="/shares"' not in home.text
    assert 'formaction="/export/bibtex"' in home.text  # exports stay available

    page = anon_client.get(f"/articles/{article_id}")
    assert page.status_code == 200
    assert "Hide this publication" not in page.text
    assert "/replace-pdf" not in page.text
    assert f'action="/articles/{article_id}/highlights"' not in page.text

    assert anon_client.get(f"/articles/{article_id}/bibtex").status_code == 200
    assert anon_client.get("/topics").status_code == 200
    assert "Create topic" not in anon_client.get("/topics").text


def test_logged_in_user_sees_name_and_edit_controls(client):
    article_id = _add_paper(client)
    page = client.get(f"/articles/{article_id}")
    assert TEST_USER.display_name in page.text
    assert "Log out" in page.text
    assert 'href="/me" class="user-chip' in page.text
    assert '<span class="user-avatar" aria-hidden="true">AT</span>' in page.text
    assert "Hide this publication" in page.text


def test_visitor_may_export_a_selection(client, anon_client):
    article_id = _add_paper(client)
    response = anon_client.post("/export/bibtex", data={"ids": [article_id]})
    assert response.status_code == 200


# --------------------------------------------------------------------------- writing
@pytest.mark.parametrize(
    "path",
    ["/api/ingest", "/topics", "/shares", "/articles/1/hide", "/api/rescan-pdfs"],
)
def test_visitor_cannot_change_anything(anon_client, path):
    browser = anon_client.post(
        path,
        data={"name": "x"},
        headers={"accept": "text/html", "referer": "http://testserver/articles/1"},
        follow_redirects=False,
    )
    assert browser.status_code == 303
    assert browser.headers["location"] == "/login?next=%2Farticles%2F1"

    script = anon_client.post(path, data={"name": "x"}, headers={"accept": "application/json"})
    assert script.status_code == 401


def test_htmx_poll_stays_open_and_htmx_writes_redirect(client, anon_client):
    article_id = _add_paper(client)
    assert anon_client.get(f"/api/articles/{article_id}").status_code == 200
    response = anon_client.post(
        f"/articles/{article_id}/hide",
        headers={"HX-Request": "true", "HX-Current-URL": f"http://testserver/articles/{article_id}"},
    )
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == f"/login?next=%2Farticles%2F{article_id}"


def test_upload_page_sends_visitor_to_login(anon_client):
    response = anon_client.get("/upload", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fupload"


def test_api_v1_keeps_its_own_key_check(anon_client):
    # No REFBASE_API_KEY in tests, so the API is open — the login gate must not
    # stand in front of it.
    assert anon_client.get("/api/v1/publications").status_code == 200


def test_added_by_comes_from_the_directory(client):
    _add_paper(client)
    from app import db
    from app.models import Article

    with db.SessionLocal() as session:
        article = session.scalar(select(Article).where(Article.doi == DOI))
        assert article.added_by == TEST_USER.display_name
    assert 'name="added_by"' not in client.get("/upload").text


# --------------------------------------------------------------------------- login
def test_login_page_uses_refbase_layout_and_rejects_wrong_password(anon_client):
    page = anon_client.get("/login?next=/upload")
    assert page.status_code == 200
    assert "<strong>RefBase</strong>" in page.text
    assert 'value="/upload"' in page.text

    wrong = anon_client.post("/login", data={"username": "tester", "password": "nope"})
    assert wrong.status_code == 401
    assert "Wrong username or password" in wrong.text


def test_login_stores_directory_snapshot(anon_client):
    response = anon_client.post(
        "/login",
        data={"username": "tester", "password": TEST_PASSWORD, "next": "/upload"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/upload"
    from app import db
    from app.models import User

    with db.SessionLocal() as session:
        user = session.get(User, "tester")
        assert user.display_name == TEST_USER.display_name
        assert user.department == "UZ3"
        assert user.groups == [{"dn": TEST_GROUP_DN, "name": "Pracownicy - UZ3"}]
    assert anon_client.get("/upload").status_code == 200

    anon_client.post("/logout")
    assert anon_client.get("/upload", follow_redirects=False).status_code == 303


# --------------------------------------------------------------------------- projects
def test_visitor_sees_no_projects(client, anon_client):
    project_id = _make_project("Open legacy", None, "1")
    for path in ("/projects", f"/projects/{project_id}", "/projects/new"):
        response = anon_client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login")


def test_projects_are_filtered_by_ad_group(client):
    mine = _make_project("Mine", TEST_GROUP_DN, "1")
    theirs = _make_project("Theirs", OTHER_GROUP_DN, "2")
    legacy = _make_project("Legacy", None, "3")

    listing = client.get("/projects").text
    assert "Mine" in listing and "Legacy" in listing
    assert "Theirs" not in listing

    assert client.get(f"/projects/{mine}").status_code == 200
    assert client.get(f"/projects/{legacy}").status_code == 200
    assert client.get(f"/projects/{theirs}").status_code == 404
    assert (
        client.post(f"/projects/{theirs}/project-number", data={"project_number": "9"})
        .status_code
        == 404
    )


def test_foreign_project_documents_are_not_downloadable(client):
    from app import db
    from app.models import DeliverableDocument

    theirs = _make_project("Theirs", OTHER_GROUP_DN, "2")
    path = db.PROJECT_DOCUMENT_DIR / "auth-test.pdf"
    path.write_bytes(b"%PDF-1.4 secret")
    with db.SessionLocal() as session:
        document = DeliverableDocument(
            source_system="refbase",
            local_project_id=theirs,
            external_project_id=theirs,
            external_entity_key="D1",
            title="Secret",
            file_path=str(path),
        )
        session.add(document)
        session.commit()
        document_id = document.id
    assert client.get(f"/project-documents/{document_id}/file").status_code == 404


def test_new_project_needs_one_of_my_groups(client):
    form = {"name": "Atlas", "project_number": "101", "description": ""}
    assert client.post("/projects", data=form).status_code == 422
    assert (
        client.post("/projects", data={**form, "ad_group_dn": OTHER_GROUP_DN}).status_code
        == 422
    )
    assert "Pracownicy - UZ3" in client.get("/projects/new").text

    created = client.post(
        "/projects", data={**form, "ad_group_dn": TEST_GROUP_DN}, follow_redirects=False
    )
    assert created.status_code == 303
    from app import db
    from app.models import Project

    with db.SessionLocal() as session:
        project = session.scalar(select(Project).where(Project.name == "Atlas"))
        assert project.ad_group_dn == TEST_GROUP_DN
        assert project.ad_group_name == "Pracownicy - UZ3"


def test_legacy_project_can_be_given_only_my_group(client):
    legacy = _make_project("Legacy", None, "3")
    refused = client.post(f"/projects/{legacy}/group", data={"ad_group_dn": OTHER_GROUP_DN})
    assert refused.status_code == 422
    done = client.post(
        f"/projects/{legacy}/group", data={"ad_group_dn": TEST_GROUP_DN}, follow_redirects=False
    )
    assert done.status_code == 303
    page = client.get(done.headers["location"]).text
    assert "Project group updated" in page
    assert f'value="{TEST_GROUP_DN}" selected' in page
