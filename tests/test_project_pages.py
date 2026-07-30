import asyncio
from pathlib import Path

from sqlalchemy import select
from starlette.requests import Request


def _request(path: str, method: str = "GET") -> Request:
    route_path, _, query_string = path.partition("?")
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": route_path,
            "raw_path": route_path.encode(),
            "query_string": query_string.encode(),
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "root_path": "",
        }
    )


class _Upload:
    filename = "architecture-report.pdf"
    content_type = "application/pdf"

    async def read(self) -> bytes:
        return b"%PDF-1.4 project report"


def test_create_project_then_upload_classified_document(client) -> None:
    from app import db
    from app.main import create_project, upload_project_document
    from app.models import DeliverableDocument, Project

    with db.SessionLocal() as session:
        response = create_project(
            _request("/projects", "POST"),
            "Project Atlas",
            "ATLAS",
            "101000001",
            "A project-document workspace.",
            session,
        )
        assert response.status_code == 303
        project = session.scalar(select(Project).where(Project.acronym == "ATLAS"))
        project_id = project.id

    with db.SessionLocal() as session:
        response = asyncio.run(
            upload_project_document(
                _request(f"/projects/{project_id}/documents", "POST"),
                project_id,
                _Upload(),
                "deliverable",
                "D2.1",
                "Architecture report",
                "First architecture deliverable.",
                session,
            )
        )
        assert response.status_code == 303

    with db.SessionLocal() as session:
        document = session.scalar(
            select(DeliverableDocument).where(
                DeliverableDocument.local_project_id == project_id
            )
        )
        assert document.external_entity_key == "D2.1"
        assert document.original_filename == "architecture-report.pdf"
        assert document.byte_size == len(b"%PDF-1.4 project report")
        assert document.file_path is not None
        assert Path(document.file_path).parent == db.PROJECT_DOCUMENT_DIR
        assert Path(document.file_path).is_file()


def test_project_page_separates_deliverables_and_milestones(client) -> None:
    from app import db
    from app.main import project_detail
    from app.models import DeliverableDocument, MilestoneDocument, Project

    with db.SessionLocal() as session:
        project = Project(name="Classification project")
        session.add(project)
        session.flush()
        session.add_all(
            [
                DeliverableDocument(
                    source_system="refbase",
                    local_project_id=project.id,
                    external_project_id=project.id,
                    external_entity_key="D1",
                    title="Deliverable example",
                    authors_json='["First Author", "Second Author"]',
                    published_date="2026-07-01",
                    lead_beneficiary="NCBJ",
                    original_filename="internal-deliverable-filename.pdf",
                ),
                MilestoneDocument(
                    source_system="refbase",
                    local_project_id=project.id,
                    external_project_id=project.id,
                    external_entity_key="MS1",
                    title="Milestone example",
                ),
            ]
        )
        session.commit()

        response = project_detail(
            _request(f"/projects/{project.id}"),
            project.id,
            session,
        )
        page = response.body.decode()

    assert '<span class="panel-title">Deliverables</span>' in page
    assert "Deliverable example" in page
    assert '<span class="panel-title">Milestones</span>' in page
    assert "Milestone example" in page
    assert "<th>Authors</th>" in page
    assert "<th>Released</th>" in page
    assert "<th>Lead beneficiary</th>" in page
    assert "First Author, Second Author" in page
    assert "2026-07-01" in page
    assert "NCBJ" in page
    assert "internal-deliverable-filename.pdf" not in page
    assert 'id="project-dropzone"' in page
    assert "Drag &amp; drop a document" in page
    assert f'action="/projects/{project.id}/project-number"' in page
    assert "Update number" in page


def test_update_project_number(client) -> None:
    from app import db
    from app.main import project_detail, update_project_number
    from app.models import Project

    with db.SessionLocal() as session:
        project = Project(name="Number change", project_number="101000001")
        session.add(project)
        session.commit()
        project_id = project.id

    with db.SessionLocal() as session:
        response = update_project_number(
            _request(f"/projects/{project_id}/project-number", "POST"),
            project_id,
            " 101000002 ",
            session,
        )
        assert response.status_code == 303
        assert response.headers["location"] == (
            f"/projects/{project_id}?project_number_updated=1"
        )
        assert session.get(Project, project_id).project_number == "101000002"

    with db.SessionLocal() as session:
        page = project_detail(
            _request(response.headers["location"]),
            project_id,
            session,
        ).body.decode()
    assert "Project number updated." in page
    assert 'value="101000002"' in page


def test_update_project_number_rejects_duplicate(client) -> None:
    from app import db
    from app.main import update_project_number
    from app.models import Project

    with db.SessionLocal() as session:
        project = Project(name="Number change", project_number="101000001")
        existing = Project(name="Existing project", project_number="101000002")
        session.add_all([project, existing])
        session.commit()
        project_id = project.id

    with db.SessionLocal() as session:
        response = update_project_number(
            _request(f"/projects/{project_id}/project-number", "POST"),
            project_id,
            "101000002",
            session,
        )
        assert response.status_code == 409
        page = response.body.decode()
        assert "Another project already uses number 101000002." in page
        assert 'value="101000002"' in page
        assert session.get(Project, project_id).project_number == "101000001"


def test_update_project_number_requires_value(client) -> None:
    from app import db
    from app.main import update_project_number
    from app.models import Project

    with db.SessionLocal() as session:
        project = Project(name="Number change", project_number="101000001")
        session.add(project)
        session.commit()
        project_id = project.id

    with db.SessionLocal() as session:
        response = update_project_number(
            _request(f"/projects/{project_id}/project-number", "POST"),
            project_id,
            "   ",
            session,
        )
        assert response.status_code == 422
        assert "Project number is required." in response.body.decode()
        assert session.get(Project, project_id).project_number == "101000001"


def test_dashboard_contains_create_project_button() -> None:
    from app.templating import TEMPLATES_DIR

    template = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    assert 'href="/projects/new">Create a project</a>' in template


def test_pdf_project_number_routes_document_to_matching_project(client) -> None:
    from app import db
    from app.main import upload_project_document
    from app.models import MilestoneDocument, Project

    pdf_path = (
        Path(__file__).parent.parent
        / "TREASURE_047_MILESTONE_12_REFRACTORY_V5_final5_signed.pdf"
    )

    class PdfUpload:
        filename = pdf_path.name
        content_type = "application/pdf"

        async def read(self) -> bytes:
            return pdf_path.read_bytes()

    with db.SessionLocal() as session:
        wrong_project = Project(
            name="Wrong target",
            project_number="999999999",
        )
        treasure = Project(
            name="TREASURE",
            acronym="TREASURE",
            project_number="101164616",
        )
        session.add_all([wrong_project, treasure])
        session.commit()
        wrong_id, treasure_id = wrong_project.id, treasure.id

    with db.SessionLocal() as session:
        response = asyncio.run(
            upload_project_document(
                _request(f"/projects/{wrong_id}/documents", "POST"),
                wrong_id,
                PdfUpload(),
                "deliverable",
                None,
                None,
                None,
                session,
            )
        )
        assert response.status_code == 303
        assert response.headers["location"] == f"/projects/{treasure_id}"

    with db.SessionLocal() as session:
        document = session.scalar(
            select(MilestoneDocument).where(
                MilestoneDocument.local_project_id == treasure_id
            )
        )
        assert document.external_entity_key == "MS12"
        assert document.title == (
            "REFRACTORY V5 core layout and its preliminary verification calculations"
        )
        assert document.lead_beneficiary == "HUN-REN EK"
        assert document.published_date == "2026-06-05"
        assert len(document.project_authors) == 10
        assert document.parse_status == "parsed"
        assert document.parsing_warnings == []
