import pytest

from backend.app.candidate_engine import (
    CandidateBlock,
    CandidateError,
    analyze_candidates,
    blocks_for_document,
    extract_word_candidates,
)
from backend.app.document_adapters import (
    DocumentCell,
    ParsedDocument,
    WorksheetContent,
    parse_document,
)


def test_pattern_candidates_use_spread_priorities_and_keep_similar_terms_separate():
    parsed = parse_document(
        b"Contact Alex Tan at alex.tan@example.test or +1 (415) 555-0199 on May 7, 2026. "
        b"Reference CASE-2026-ABC; Alex Tann reviewed it.",
        ".txt",
    )

    candidates = analyze_candidates(
        [CandidateBlock("text", parsed.text)],
        "doc-1",
        "version-1",
    )
    by_term = {candidate["term"]: candidate for candidate in candidates}

    assert by_term["alex.tan@example.test"]["category"] == "EMAIL"
    assert by_term["alex.tan@example.test"]["level"] == 2
    assert by_term["+1 (415) 555-0199"]["category"] == "PHONE"
    assert by_term["+1 (415) 555-0199"]["level"] == 3
    assert by_term["May 7, 2026"]["category"] == "DATE"
    assert by_term["May 7, 2026"]["level"] == 10
    assert by_term["CASE-2026-ABC"]["category"] == "IDENTIFIER"
    assert by_term["CASE-2026-ABC"]["level"] == 4
    assert by_term["Alex Tan"]["level"] == 8
    assert {candidate["level"] for candidate in candidates} >= {2, 3, 4, 8, 10}
    assert by_term["Alex Tan"]["occurrences"][0] == {
        "location": "text",
        "start": 8,
        "end": 16,
    }
    assert by_term["Alex Tan"]["id"] != by_term["Alex Tann"]["id"]
    repeated_candidates = analyze_candidates(
        [CandidateBlock("text", parsed.text)],
        "doc-1",
        "version-1",
    )
    assert repeated_candidates == candidates


def test_candidate_discovery_callback_emits_words_as_the_scan_finds_them():
    discovered = []
    candidates = analyze_candidates(
        [CandidateBlock("text", "Alex Tan can be reached at alex@example.test.")],
        "doc-1",
        "version-1",
        on_candidate=discovered.append,
    )

    assert {candidate["term"] for candidate in discovered} >= {"Alex Tan", "alex@example.test"}
    assert all(candidate["scoreStatus"] == "scanning" for candidate in discovered)
    assert {candidate["id"] for candidate in discovered} <= {candidate["id"] for candidate in candidates}


def test_stage_one_extracts_unique_words_document_wide_and_splits_punctuation_hyphens_and_underscores():
    candidates = extract_word_candidates(
        [
            CandidateBlock("first", "alpha-beta_under jumbledword, alpha"),
            CandidateBlock("second", "BETA under"),
        ],
        "doc-1",
        "version-1",
    )
    by_term = {candidate["term"].casefold(): candidate for candidate in candidates}

    assert set(by_term) == {"alpha", "beta", "under", "jumbledword"}
    assert by_term["alpha"]["occurrenceCount"] == 2
    assert by_term["beta"]["occurrenceCount"] == 2
    assert by_term["under"]["occurrenceCount"] == 2
    assert all(candidate["category"] == "WORD" for candidate in candidates)


def test_manual_phrase_is_included_and_pinned_and_decisions_survive_analysis():
    blocks = [CandidateBlock("text", "Project Cedar; project cedar remains internal.")]
    candidates = analyze_candidates(blocks, "doc-1", "version-1", manual_terms=["Project Cedar"])
    manual = next(candidate for candidate in candidates if candidate["term"].casefold() == "project cedar")

    assert manual["source"] == "manual"
    assert manual["decision"] == "included"
    assert manual["pinned"] is True
    assert manual["occurrenceCount"] == 2

    manual["decision"] = "excluded"
    manual["pinned"] = True
    repeated = analyze_candidates(blocks, "doc-1", "version-1", [manual])
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

    candidates = analyze_candidates(blocks, "doc-1", "version-1", ner_entities=entities)
    by_term = {candidate["term"]: candidate for candidate in candidates}

    assert by_term["Alex Tan"]["source"] == "ner"
    assert by_term["Alex Tan"]["category"] == "NER_ENTITY"
    assert by_term["Alex Tan"]["nerLabels"] == ["person"]
    assert by_term["Alex Tan"]["nerScore"] == 0.91
    assert by_term["Alex Tan"]["level"] == 2
    assert by_term["Example Corp"]["source"] == "ner"
    assert by_term["Example Corp"]["level"] == 5
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
    first = analyze_candidates([CandidateBlock("text", "Alex Tan")], "doc-1", "version-1")
    second = analyze_candidates([CandidateBlock("text", "Alex Tan")], "doc-1", "version-2")

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
