import pytest

from backend.app.candidate_engine import (
    CandidateBlock,
    CandidateError,
    analyze_candidates,
    blocks_for_document,
)
from backend.app.document_adapters import (
    DocumentCell,
    ParsedDocument,
    WorksheetContent,
    parse_document,
)


def test_pattern_candidates_record_occurrences_tiers_and_unconfirmed_proposals():
    parsed = parse_document(
        b"Contact Alex Tan at alex.tan@example.test or +1 (415) 555-0199 on May 7, 2026. "
        b"Reference CASE-2026-ABC; Alex Tann reviewed it.",
        ".txt",
    )

    candidates, proposals = analyze_candidates(
        [CandidateBlock("text", parsed.text)],
        "doc-1",
        "version-1",
    )
    by_term = {candidate["term"]: candidate for candidate in candidates}

    assert by_term["alex.tan@example.test"]["category"] == "EMAIL"
    assert by_term["alex.tan@example.test"]["level"] == 1
    assert by_term["+1 (415) 555-0199"]["category"] == "PHONE"
    assert by_term["May 7, 2026"]["category"] == "DATE"
    assert by_term["CASE-2026-ABC"]["category"] == "IDENTIFIER"
    assert by_term["Alex Tan"]["occurrences"][0] == {
        "location": "text",
        "start": 8,
        "end": 16,
    }
    assert proposals
    assert all(proposal["status"] == "proposed" and proposal["confirmed"] is False for proposal in proposals)
    assert all(proposal["score"] >= 88 for proposal in proposals)
    repeated_candidates, repeated_proposals = analyze_candidates(
        [CandidateBlock("text", parsed.text)],
        "doc-1",
        "version-1",
    )
    assert repeated_candidates == candidates
    assert repeated_proposals == proposals


def test_manual_phrase_is_included_and_pinned_and_decisions_survive_analysis():
    blocks = [CandidateBlock("text", "Project Cedar; project cedar remains internal.")]
    candidates, _ = analyze_candidates(blocks, "doc-1", "version-1", manual_terms=["Project Cedar"])
    manual = next(candidate for candidate in candidates if candidate["term"].casefold() == "project cedar")

    assert manual["source"] == "manual"
    assert manual["decision"] == "included"
    assert manual["pinned"] is True
    assert manual["occurrenceCount"] == 2

    manual["decision"] = "excluded"
    manual["pinned"] = True
    repeated, _ = analyze_candidates(blocks, "doc-1", "version-1", [manual])
    preserved = next(candidate for candidate in repeated if candidate["id"] == manual["id"])

    assert preserved["decision"] == "excluded"
    assert preserved["pinned"] is True


def test_ner_entities_become_suggestions_and_enrich_matching_pattern_candidates():
    blocks = [CandidateBlock("text", "Alex Tan works at Example Corp.")]
    entities = [
        {"text": "Alex Tan", "location": "text", "start": 0, "end": 8, "label": "person", "score": 0.91},
        {"text": "Example Corp", "location": "text", "start": 18, "end": 30, "label": "organization", "score": 0.88},
        {"text": "Wrong text", "location": "text", "start": 0, "end": 10, "label": "person", "score": 0.99},
    ]

    candidates, _ = analyze_candidates(blocks, "doc-1", "version-1", ner_entities=entities)
    by_term = {candidate["term"]: candidate for candidate in candidates}

    assert by_term["Alex Tan"]["source"] == "ner"
    assert by_term["Alex Tan"]["category"] == "NER_ENTITY"
    assert by_term["Alex Tan"]["nerLabels"] == ["person"]
    assert by_term["Alex Tan"]["nerScore"] == 0.91
    assert by_term["Example Corp"]["source"] == "ner"
    assert "Wrong text" not in by_term


def test_manual_phrase_must_exist_in_supported_text():
    with pytest.raises(CandidateError, match="not present"):
        analyze_candidates(
            [CandidateBlock("text", "Nothing selected here.")],
            "doc-1",
            "version-1",
            manual_terms=["Absent phrase"],
        )


def test_candidate_decisions_are_version_scoped():
    first, _ = analyze_candidates([CandidateBlock("text", "Alex Tan")], "doc-1", "version-1")
    second, _ = analyze_candidates([CandidateBlock("text", "Alex Tan")], "doc-1", "version-2")

    assert first[0]["id"] != second[0]["id"]


def test_supported_spreadsheet_and_office_formats_map_to_editable_text_blocks():
    csv = ParsedDocument(
        format="CSV",
        encoding="utf-8",
        source=b"",
        rows=(("Contact", "alex@example.test"),),
    )
    xlsx = ParsedDocument(
        format="XLSX",
        encoding="xml-utf-8",
        source=b"",
        sheets=(WorksheetContent("Contacts", (DocumentCell("B4", "Alex Tan"),)),),
    )
    docx = ParsedDocument(
        format="DOCX",
        encoding="xml-utf-8",
        source=b"",
        text="Alex Tan\nProject Cedar",
    )

    assert [(block.location, block.text) for block in blocks_for_document(csv)] == [
        ("row:1,column:1", "Contact"),
        ("row:1,column:2", "alex@example.test"),
    ]
    assert [(block.location, block.text) for block in blocks_for_document(xlsx)] == [
        ("sheet:Contacts,cell:B4", "Alex Tan"),
    ]
    assert [(block.location, block.text) for block in blocks_for_document(docx)] == [
        ("paragraph:0", "Alex Tan"),
        ("paragraph:1", "Project Cedar"),
    ]
