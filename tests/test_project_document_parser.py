from pathlib import Path


def test_treasure_milestone_metadata_is_parsed() -> None:
    from app.project_document_parser import parse_project_document_pdf

    pdf = (
        Path(__file__).parent.parent
        / "TREASURE_047_MILESTONE_12_REFRACTORY_V5_final5_signed.pdf"
    )
    parsed = parse_project_document_pdf(pdf.read_bytes())

    assert parsed.project_number == "101164616"
    assert parsed.document_type == "milestone"
    assert parsed.entity_key == "MS12"
    assert parsed.title == (
        "REFRACTORY V5 core layout and its preliminary verification calculations"
    )
    assert parsed.lead_beneficiary == "HUN-REN EK"
    assert parsed.authors == [
        "P. Pónya",
        "G. Mayer",
        "Zs. Bali",
        "A. Guba",
        "I. Pataki",
        "D. Sebestény",
        "Z. I. Böröczki",
        "I. Panka",
        "Á. Horváth",
        "E. Slonszki",
    ]
    assert parsed.published_date == "2026-06-05"
    assert parsed.warnings == []
    assert parsed.full_text and "Project Number 101164616" in parsed.full_text
