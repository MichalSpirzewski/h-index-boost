import pytest
from fastapi import HTTPException
from pydantic import ValidationError


@pytest.fixture()
def api_session(client):
    from app import db

    with db.SessionLocal() as session:
        yield session


def test_publication_metadata_api_lifecycle(api_session) -> None:
    from app.api_v1 import (
        PublicationCreate,
        PublicationUpdate,
        archive_publication,
        create_publication,
        get_publication,
        list_publications,
        update_publication,
    )

    publication = create_publication(
        PublicationCreate(
            publication_type="conference",
            title="A conference API paper",
            doi="https://doi.org/10.1234/API.TEST",
            year=2026,
            conference_name="Research API Conference",
            proceedings_title="Proceedings of RAC 2026",
        ),
        api_session,
    )
    assert publication.publication_type == "conference"
    assert publication.doi == "10.1234/api.test"
    assert publication.venue_name == "Research API Conference"

    fetched = get_publication(publication.id, api_session)
    assert fetched.title == "A conference API paper"

    updated = update_publication(
        publication.id,
        PublicationUpdate(title="Updated conference paper"),
        api_session,
    )
    assert updated.title == "Updated conference paper"

    listed = list_publications(api_session, "conference", False)
    assert publication.id in [item.id for item in listed]

    archived = archive_publication(publication.id, api_session)
    assert archived.hidden is True


def test_project_document_metadata_api_lifecycle(api_session) -> None:
    from app.api_v1 import (
        ProjectDocumentCreate,
        ProjectDocumentUpdate,
        archive_project_document,
        create_project_document,
        get_project_document,
        list_project_documents,
        update_project_document,
    )

    document = create_project_document(
        ProjectDocumentCreate(
            document_type="deliverable",
            external_project_id="project-api-123",
            external_entity_key="D2.1",
            title="Architecture report",
        ),
        api_session,
    )
    assert document.document_type == "deliverable"
    assert document.external_entity_key == "D2.1"

    fetched = get_project_document(document.id, api_session)
    assert fetched.external_project_id == "project-api-123"

    updated = update_project_document(
        document.id,
        ProjectDocumentUpdate(description="First accepted project report"),
        api_session,
    )
    assert updated.description == "First accepted project report"

    listed = list_project_documents(
        "project-api-123", api_session, None, False, "doc-workflow"
    )
    assert [item.id for item in listed] == [document.id]

    with pytest.raises(HTTPException) as duplicate:
        create_project_document(
            ProjectDocumentCreate(
                document_type="deliverable",
                external_project_id="project-api-123",
                external_entity_key="D2.1",
            ),
            api_session,
        )
    assert duplicate.value.status_code == 409

    archived = archive_project_document(document.id, api_session)
    assert archived.status == "archived"
    assert archived.archived_at is not None

    assert (
        list_project_documents(
            "project-api-123", api_session, None, False, "doc-workflow"
        )
        == []
    )
    included = list_project_documents(
        "project-api-123", api_session, None, True, "doc-workflow"
    )
    assert [item.id for item in included] == [document.id]


def test_api_key_is_enforced_when_configured(monkeypatch) -> None:
    from app.api_v1 import require_api_key

    monkeypatch.setenv("REFBASE_API_KEY", "test-secret")

    with pytest.raises(HTTPException) as unauthorized:
        require_api_key(None)
    assert unauthorized.value.status_code == 401

    require_api_key("test-secret")


def test_api_rejects_unknown_types_and_fields() -> None:
    from app.api_v1 import ProjectDocumentCreate, PublicationCreate

    with pytest.raises(ValidationError):
        ProjectDocumentCreate(
            document_type="report",
            external_project_id="project-api-123",
            external_entity_key="R1",
        )

    with pytest.raises(ValidationError):
        PublicationCreate(
            publication_type="journal",
            title="Strict payload",
            unexpected="value",
        )


def test_versioned_routes_are_in_openapi(client) -> None:
    from app.main import app

    paths = app.openapi()["paths"]
    assert "/api/v1/publications" in paths
    assert "/api/v1/publications/{publication_id}" in paths
    assert "/api/v1/project-documents" in paths
    assert "/api/v1/project-documents/{document_id}" in paths
    assert "/api/v1/projects/{external_project_id}/documents" in paths
