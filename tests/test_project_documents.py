from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError


def test_project_document_subtypes_round_trip_polymorphically(client) -> None:
    from app import db
    from app.models import (
        DeliverableDocument,
        MilestoneDocument,
        ProjectDocument,
    )

    with db.SessionLocal() as session:
        deliverable = DeliverableDocument(
            external_project_id="project-123",
            external_entity_key="D2.1",
            title="Architecture report",
        )
        milestone = MilestoneDocument(
            external_project_id="project-123",
            external_entity_key="MS3",
            title="Prototype validated",
        )
        session.add_all([deliverable, milestone])
        session.commit()
        deliverable_id, milestone_id = deliverable.id, milestone.id

    with db.SessionLocal() as session:
        documents = {
            document.id: document
            for document in session.scalars(select(ProjectDocument)).all()
        }

    assert isinstance(documents[deliverable_id], DeliverableDocument)
    assert documents[deliverable_id].document_type == "deliverable"
    assert isinstance(documents[milestone_id], MilestoneDocument)
    assert documents[milestone_id].document_type == "milestone"


def test_project_document_uses_api_safe_uuid(client) -> None:
    from uuid import UUID

    from app import db
    from app.models import DeliverableDocument

    with db.SessionLocal() as session:
        document = DeliverableDocument(
            external_project_id="project-123",
            external_entity_key="D1.1",
        )
        session.add(document)
        session.commit()

        assert str(UUID(document.id)) == document.id


def test_external_project_entity_link_is_unique(client) -> None:
    from app import db
    from app.models import DeliverableDocument

    with db.SessionLocal() as session:
        session.add(
            DeliverableDocument(
                external_project_id="project-123",
                external_entity_key="D4.2",
            )
        )
        session.commit()
        session.add(
            DeliverableDocument(
                external_project_id="project-123",
                external_entity_key="D4.2",
            )
        )

        try:
            session.commit()
        except IntegrityError:
            session.rollback()
        else:
            raise AssertionError("duplicate external entity link was accepted")


def test_project_documents_table_is_initialized(client) -> None:
    from app import db

    with db.engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(project_documents)"))
        }

    assert {
        "id",
        "document_type",
        "source_system",
        "external_project_id",
        "external_entity_key",
        "title",
        "description",
        "lead_beneficiary",
        "authors_json",
        "published_date",
        "pdf_text",
        "parse_status",
        "parse_warnings",
        "status",
        "archived_at",
        "created_at",
    } <= columns
