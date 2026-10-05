import pytest

from backend.app.candidate_engine import (
    CandidateBlock,
    CandidateError,
    analyze_candidates,
    blocks_for_document,
    merge_proposals,
    propose_contextual_variants,
    propose_variants,
)
from backend.app.document_adapters import (
    DocumentCell,
    ParsedDocument,
    WorksheetContent,
    parse_document,
)


def test_pattern_candidates_use_spread_priorities_occurrences_and_unconfirmed_proposals():
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


def test_candidate_discovery_callback_emits_words_as_the_scan_finds_them():
    discovered = []
    candidates, _ = analyze_candidates(
        [CandidateBlock("text", "Alex Tan can be reached at alex@example.test.")],
        "doc-1",
        "version-1",
        on_candidate=discovered.append,
    )

    assert {candidate["term"] for candidate in discovered} >= {"Alex Tan", "alex@example.test"}
    assert all(candidate["scoreStatus"] == "scanning" for candidate in discovered)
    assert {candidate["id"] for candidate in discovered} <= {candidate["id"] for candidate in candidates}


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
    assert by_term["Alex Tan"]["level"] == 2
    assert by_term["Example Corp"]["source"] == "ner"
    assert by_term["Example Corp"]["level"] == 5
    assert "Wrong text" not in by_term


def test_contextual_proposals_embed_masked_context_and_remain_unconfirmed():
    text = (
        "Alex Tan, the senior engineer, signed the confidential report. "
        "Jordan Lee, the senior engineer, approved the final budget."
    )
    block = CandidateBlock("text", text)
    candidates, _ = analyze_candidates([block], "doc-1", "version-1")
    embedded_contexts = []

    def embed(contexts):
        embedded_contexts.extend(contexts)
        return [[1.0, 0.0] if "senior engineer" in context else [0.0, 1.0] for context in contexts]

    proposals = propose_contextual_variants(candidates, [block], embed)
    by_term = {candidate["term"]: candidate for candidate in candidates}
    name_pair = {by_term["Alex Tan"]["id"], by_term["Jordan Lee"]["id"]}
    proposal = next(
        proposal for proposal in proposals
        if {proposal["sourceId"], proposal["targetId"]} == name_pair
    )

    assert embedded_contexts
    assert all("Alex Tan" not in context and "Jordan Lee" not in context for context in embedded_contexts)
    assert proposal["method"] == "minilm"
    assert proposal["score"] == 100.0
    assert proposal["status"] == "proposed"
    assert proposal["confirmed"] is False
    assert "masked" in proposal["reason"]


def test_contextual_proposals_skip_mentions_without_unmasked_context():
    block = CandidateBlock("text", "Alex Tan\nJordan Lee")
    candidates, _ = analyze_candidates([block], "doc-1", "version-1")

    assert all("\n" not in candidate["term"] for candidate in candidates)

    def unused_embedder(contexts):
        pytest.fail(f"No useful context should be encoded: {contexts}")

    assert propose_contextual_variants(candidates, [block], unused_embedder) == []


def test_contextual_proposals_reject_unrelated_contexts_below_cosine_threshold():
    block = CandidateBlock(
        "text",
        "Alex Tan reported a network outage. Jordan Lee enjoys gardening.",
    )
    candidates, _ = analyze_candidates([block], "doc-1", "version-1")

    proposals = propose_contextual_variants(
        candidates,
        [block],
        lambda contexts: [
            [1.0, 0.0] if "network outage" in context else [0.0, 1.0]
            for context in contexts
        ],
    )

    names = {candidate["term"]: candidate["id"] for candidate in candidates}
    assert not any(
        {proposal["sourceId"], proposal["targetId"]} == {names["Alex Tan"], names["Jordan Lee"]}
        for proposal in proposals
    )


def test_rapidfuzz_and_minilm_evidence_merge_into_one_review_edge():
    nodes = [
        {"id": "candidate-a", "term": "Alex Tan"},
        {"id": "candidate-b", "term": "Alex Tann"},
    ]
    lexical = propose_variants(nodes)
    contextual = [{
        "id": "minilm-edge",
        "sourceId": "candidate-a",
        "targetId": "candidate-b",
        "score": 91.0,
        "method": "minilm",
        "scores": {"minilm": 91.0},
        "reason": "MiniLM contextual similarity (91.0% cosine; candidate mentions masked)",
        "status": "proposed",
        "confirmed": False,
    }]

    merged = merge_proposals(lexical, contextual)

    assert len(merged) == 1
    assert merged[0]["method"] == "combined"
    assert merged[0]["scores"] == {"rapidfuzz": lexical[0]["score"], "minilm": 91.0}
    assert merged[0]["methods"] == ["minilm", "rapidfuzz"]
    assert merged[0]["confirmed"] is False


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
