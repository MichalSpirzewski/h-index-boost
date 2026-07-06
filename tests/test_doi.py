import pytest

from app.ingest import extract_doi, normalize_doi


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10.1038/nphys1170", "10.1038/nphys1170"),
        ("10.1038/NPHYS1170", "10.1038/nphys1170"),
        ("  10.1038/nphys1170 \n", "10.1038/nphys1170"),
        ("https://doi.org/10.1038/nphys1170", "10.1038/nphys1170"),
        ("http://doi.org/10.1038/nphys1170", "10.1038/nphys1170"),
        ("https://dx.doi.org/10.1038/nphys1170", "10.1038/nphys1170"),
        ("http://dx.doi.org/10.1038/nphys1170", "10.1038/nphys1170"),
        ("doi:10.1038/nphys1170", "10.1038/nphys1170"),
        ("DOI:10.1038/NPHYS1170", "10.1038/nphys1170"),
    ],
)
def test_normalize_doi(raw: str, expected: str) -> None:
    assert normalize_doi(raw) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10.1038/nphys1170", "10.1038/nphys1170"),
        ("https://doi.org/10.1038/NPHYS1170", "10.1038/nphys1170"),
        # trailing sentence punctuation must be stripped
        ("See https://doi.org/10.1038/nphys1170. More text", "10.1038/nphys1170"),
        ("cited as 10.1038/nphys1170, among others", "10.1038/nphys1170"),
        # unbalanced closing paren stripped ...
        ("(doi: 10.1038/nphys1170)", "10.1038/nphys1170"),
        # ... but parens that are part of the DOI are kept
        (
            "doi:10.1016/S0141-0229(97)00066-2 in text",
            "10.1016/s0141-0229(97)00066-2",
        ),
        ("(see 10.1016/S0141-0229(97)00066-2).", "10.1016/s0141-0229(97)00066-2"),
        # registrant prefixes have 4-9 digits
        ("10.123456789/abc", "10.123456789/abc"),
        # no DOI at all
        ("https://www.nature.com/articles/nphys1170", None),
        ("just some words", None),
        ("", None),
        (None, None),
    ],
)
def test_extract_doi(text: str | None, expected: str | None) -> None:
    assert extract_doi(text) == expected
