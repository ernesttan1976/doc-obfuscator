from __future__ import annotations

import hashlib
import re
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from itertools import pairwise

from .document_adapters import ParsedDocument

MAX_OCCURRENCES_PER_CANDIDATE = 2_000
# Priorities run from 2 (most sensitive) to 10 (least sensitive). These are
# heuristics for filtering, not a guarantee that every occurrence was found.
CANDIDATE_PRIORITY_LEVELS = {
    "WORD": 8,
    "EMAIL": 2,
    "PHONE": 3,
    "DATE": 10,
    "IDENTIFIER": 4,
    "CAPITALIZED_PHRASE": 8,
    "MANUAL": 2,
}
NER_LABEL_PRIORITY_LEVELS = {
    "PERSON": 2,
    "EMAIL": 2,
    "PHONE": 3,
    "LOCATION": 4,
    "ADDRESS": 4,
    "IDENTIFIER": 4,
    "ACCOUNT NUMBER": 4,
    "ORGANIZATION": 6,
    "ORG": 6,
    "COMPANY": 6,
    "DATE": 10,
    "TIME": 10,
}

_EMAIL = re.compile(
    r"(?<![\w.+-])[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+(?![\w-])",
    re.IGNORECASE,
)
_PHONE = re.compile(r"(?<!\w)\+?\d[\d ().-]{5,}\d(?!\w)")
_MONTH_DATE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{2,4})?\b|"
    r"\b\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{2,4}\b",
    re.IGNORECASE,
)
_NUMERIC_DATE = re.compile(
    r"\b(?:0?[1-9]|1[0-2])[/.-](?:0?[1-9]|[12]\d|3[01])(?:[/.-](?:\d{2}|\d{4}))\b"
)
_IDENTIFIER = re.compile(r"\b(?:[A-Z]{2,}[\w]*[-_/][A-Z0-9][A-Z0-9_-]*|[A-Z]{2,}\d{2,})\b")
_CAPITALIZED_TOKEN = re.compile(r"(?<![\w])(?:[A-Z][a-z]+(?:[’'-][A-Z]?[a-z]+)*|[A-Z]\.)(?![\w])")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_COMMON_SENTENCE_STARTERS = {"a", "an", "at", "contact", "for", "from", "in", "on", "owner", "please", "reference", "the", "to"}
@dataclass(frozen=True)
class CandidateBlock:
    location: str
    text: str


class CandidateError(Exception):
    """A candidate request cannot be applied to the selected document version."""


def blocks_for_document(parsed: ParsedDocument) -> tuple[CandidateBlock, ...]:
    """Expose editable text blocks with stable adapter locations for extraction."""
    if parsed.format == "CSV":
        return tuple(
            CandidateBlock(f"row:{row_index},column:{column_index}", value)
            for row_index, row in enumerate(parsed.rows, start=1)
            for column_index, value in enumerate(row, start=1)
            if value
        )
    if parsed.format == "XLSX":
        return tuple(
            CandidateBlock(f"sheet:{sheet.name},cell:{cell.location}", cell.text)
            for sheet in parsed.sheets
            for cell in sheet.cells
            if cell.text
        )
    if parsed.text is None:
        return ()
    if parsed.format in {"DOCX", "PPTX"}:
        return tuple(
            CandidateBlock(f"paragraph:{index}", paragraph)
            for index, paragraph in enumerate(parsed.text.splitlines())
            if paragraph
        )
    return (CandidateBlock("text", parsed.text),) if parsed.text else ()


def analyze_candidates(
    blocks: Iterable[CandidateBlock],
    document_id: str,
    version_id: str,
    existing_nodes: Iterable[dict[str, object]] = (),
    manual_terms: Iterable[str] = (),
    ner_entities: Iterable[dict[str, object]] = (),
    on_candidate: Callable[[dict[str, object]], None] | None = None,
) -> list[dict[str, object]]:
    """Extract conservative local candidates without automatic similarity matching."""
    blocks = tuple(blocks)
    preserved = {
        str(node.get("term", "")).casefold(): node
        for node in existing_nodes
        if node.get("documentId") == document_id and node.get("versionId") == version_id
    }
    collected: dict[str, dict[str, object]] = {}

    def report_candidate(discovered: dict[str, object]) -> None:
        if on_candidate is None:
            return
        term = str(discovered["term"])
        category = str(discovered["category"])
        source = str(discovered["source"])
        location = str(discovered["location"])
        start = int(discovered["start"])
        end = int(discovered["end"])
        normalized_term = " ".join(term.split()).casefold()
        level = _priority_level(category, set(discovered["nerLabels"]), float(discovered["nerScore"]))
        on_candidate(
            {
                "id": _candidate_id(version_id, normalized_term),
                "documentId": document_id,
                "versionId": version_id,
                "term": term,
                "category": category,
                "level": level,
                "source": source,
                "nerLabels": discovered["nerLabels"],
                "nerScore": discovered["nerScore"],
                "occurrences": [{"location": location, "start": start, "end": end}],
                "occurrenceCount": discovered["occurrenceCount"],
                "occurrencesTruncated": False,
                "decision": "included" if source == "manual" else "suggested",
                "pinned": source == "manual",
                "scoreStatus": "scanning",
            }
        )

    for block in blocks:
        for category, pattern in (
            ("EMAIL", _EMAIL),
            ("PHONE", _PHONE),
            ("DATE", _MONTH_DATE),
            ("DATE", _NUMERIC_DATE),
            ("IDENTIFIER", _IDENTIFIER),
        ):
            for match in pattern.finditer(block.text):
                term = match.group(0).strip()
                if len(term) > 256:
                    continue
                if category == "PHONE" and not 7 <= sum(char.isdigit() for char in term) <= 15:
                    continue
                if category == "PHONE" and _overlaps_date(block.text, match.start(), match.end()):
                    continue
                _add_occurrence(
                    collected,
                    term,
                    category,
                    block.location,
                    match.start(),
                    match.end(),
                    on_candidate=report_candidate,
                )
        for start, end in _capitalized_phrase_matches(block.text):
            _add_occurrence(
                collected,
                block.text[start:end],
                "CAPITALIZED_PHRASE",
                block.location,
                start,
                end,
                on_candidate=report_candidate,
            )

    block_text_by_location = {block.location: block.text for block in blocks}
    for entity in ner_entities:
        term = str(entity.get("text", "")).strip()
        location = str(entity.get("location", ""))
        start = entity.get("start")
        end = entity.get("end")
        label = str(entity.get("label", "entity")).strip().lower()
        score = entity.get("score", 0.0)
        if (
            not term
            or len(term) > 256
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end <= start
            or location not in block_text_by_location
            or block_text_by_location[location][start:end] != term
        ):
            continue
        _add_occurrence(
            collected,
            term,
            "NER_ENTITY",
            location,
            start,
            end,
            source="ner",
            ner_label=label,
            ner_score=float(score),
            on_candidate=report_candidate,
        )

    for term in manual_terms:
        clean_term = " ".join(term.split())
        if not clean_term or len(clean_term) > 256 or any(ord(char) < 32 for char in clean_term):
            raise CandidateError("Manually selected phrases must contain 1–256 printable characters.")
        found = False
        for block in blocks:
            for match in re.finditer(re.escape(clean_term), block.text, re.IGNORECASE):
                found = True
                _add_occurrence(
                    collected,
                    match.group(0),
                    "MANUAL",
                    block.location,
                    match.start(),
                    match.end(),
                    on_candidate=report_candidate,
                )
        if not found:
            raise CandidateError("The selected phrase is not present in supported text for this version.")

    nodes: list[dict[str, object]] = []
    ordered_records = sorted(
        collected.items(),
        key=lambda item: (
            not bool(item[1]["manual"]),
            _category_priority(str(item[1]["category"])),
            item[0],
        ),
    )
    for normalized_term, record in ordered_records:
        previous = preserved.get(normalized_term, {})
        term = str(record["term"])
        node_id = _candidate_id(version_id, normalized_term)
        occurrences = record["occurrences"]
        assert isinstance(occurrences, list)
        nodes.append(
            {
                "id": node_id,
                "documentId": document_id,
                "versionId": version_id,
                "term": term,
                "category": record["category"],
                "level": _priority_level(
                    str(record["category"]),
                    record["nerLabels"],
                    float(record["nerScore"]),
                ),
                "source": (
                    "manual" if record["manual"] else "ner" if "ner" in record["sources"] else "pattern"
                ),
                "nerLabels": sorted(record["nerLabels"]),
                "nerScore": record["nerScore"],
                "occurrences": occurrences[:MAX_OCCURRENCES_PER_CANDIDATE],
                "occurrenceCount": record["occurrenceCount"],
                "occurrencesTruncated": record["occurrenceCount"] > MAX_OCCURRENCES_PER_CANDIDATE,
                "decision": previous.get("decision", "included" if record["manual"] else "suggested"),
                "pinned": bool(previous.get("pinned", record["manual"])),
            }
        )
    return nodes


def extract_word_candidates(
    blocks: Iterable[CandidateBlock],
    document_id: str,
    version_id: str,
    existing_nodes: Iterable[dict[str, object]] = (),
    manual_terms: Iterable[str] = (),
) -> list[dict[str, object]]:
    """Collect one candidate per unique document word, retaining all its locations."""
    blocks = tuple(blocks)
    preserved = {
        str(node.get("term", "")).casefold(): node
        for node in existing_nodes
        if node.get("documentId") == document_id and node.get("versionId") == version_id
    }
    collected: dict[str, dict[str, object]] = {}

    for block in blocks:
        for match in _WORD.finditer(block.text):
            term = match.group(0)
            if len(term) > 256:
                continue
            _add_occurrence(
                collected,
                term,
                "WORD",
                block.location,
                match.start(),
                match.end(),
            )

    for term in manual_terms:
        clean_term = " ".join(term.split())
        if not clean_term or len(clean_term) > 256 or any(ord(char) < 32 for char in clean_term):
            raise CandidateError("Manually selected phrases must contain 1–256 printable characters.")
        found = False
        for block in blocks:
            for match in re.finditer(re.escape(clean_term), block.text, re.IGNORECASE):
                found = True
                _add_occurrence(
                    collected,
                    match.group(0),
                    "MANUAL",
                    block.location,
                    match.start(),
                    match.end(),
                )
        if not found:
            raise CandidateError("The selected phrase is not present in supported text for this version.")

    nodes: list[dict[str, object]] = []
    ordered_records = sorted(
        collected.items(),
        key=lambda item: (
            not bool(item[1]["manual"]),
            _category_priority(str(item[1]["category"])),
            item[0],
        ),
    )
    for normalized_term, record in ordered_records:
        term = str(record["term"])
        occurrences = record["occurrences"]
        assert isinstance(occurrences, list)
        nodes.append(
            {
                "id": _candidate_id(version_id, normalized_term),
                "documentId": document_id,
                "versionId": version_id,
                "term": term,
                "category": record["category"],
                "level": _priority_level(str(record["category"]), set(), 0.0),
                "source": "manual" if record["manual"] else "word",
                "nerLabels": [],
                "nerScore": 0.0,
                "occurrences": occurrences[:MAX_OCCURRENCES_PER_CANDIDATE],
                "occurrenceCount": record["occurrenceCount"],
                "occurrencesTruncated": record["occurrenceCount"] > MAX_OCCURRENCES_PER_CANDIDATE,
                "decision": preserved.get(normalized_term, {}).get(
                    "decision", "included" if record["manual"] else "suggested"
                ),
                "pinned": bool(preserved.get(normalized_term, {}).get("pinned", record["manual"])),
            }
        )
    return nodes


def decide_candidate(nodes: list[dict[str, object]], candidate_id: str, decision: str) -> dict[str, object]:
    if decision not in {"included", "excluded", "suggested"}:
        raise CandidateError("Candidate decision must be included, excluded, or suggested.")
    for node in nodes:
        if node.get("id") == candidate_id:
            node["decision"] = decision
            node["pinned"] = decision in {"included", "excluded"}
            return node
    raise CandidateError("The selected candidate was not found in this document version.")


def _add_occurrence(
    collected: dict[str, dict[str, object]],
    term: str,
    category: str,
    location: str,
    start: int,
    end: int,
    *,
    source: str = "pattern",
    ner_label: str | None = None,
    ner_score: float = 0.0,
    on_candidate: Callable[[dict[str, object]], None] | None = None,
) -> None:
    normalized = " ".join(term.split()).casefold()
    if not normalized:
        return
    current = collected.get(normalized)
    is_new = current is None
    previous_category = str(current["category"]) if current is not None else category
    had_source = source in current["sources"] if current is not None else False
    if current is None:
        current = {
            "term": term,
            "category": category,
            "occurrences": [],
            "occurrenceCount": 0,
            "manual": category == "MANUAL",
            "sources": set(),
            "nerLabels": set(),
            "nerScore": 0.0,
        }
        collected[normalized] = current
    elif _category_priority(category) < _category_priority(str(current["category"])):
        current["category"] = category
    current["manual"] = bool(current["manual"]) or category == "MANUAL"
    sources = current["sources"]
    assert isinstance(sources, set)
    sources.add(source)
    ner_labels = current["nerLabels"]
    assert isinstance(ner_labels, set)
    if ner_label:
        ner_labels.add(ner_label)
        current["nerScore"] = max(float(current["nerScore"]), ner_score)
    occurrences = current["occurrences"]
    assert isinstance(occurrences, list)
    occurrence = {"location": location, "start": start, "end": end}
    if occurrence in occurrences:
        return
    current["occurrenceCount"] = int(current["occurrenceCount"]) + 1
    if len(occurrences) < MAX_OCCURRENCES_PER_CANDIDATE:
        occurrences.append(occurrence)
    if on_candidate is not None and (is_new or previous_category != str(current["category"]) or not had_source):
        on_candidate(
            {
                "term": str(current["term"]),
                "category": str(current["category"]),
                "source": (
                    "manual" if current["manual"] else "ner" if "ner" in sources else "pattern"
                ),
                "location": location,
                "start": start,
                "end": end,
                "nerLabels": sorted(ner_labels),
                "nerScore": current["nerScore"],
                "occurrenceCount": current["occurrenceCount"],
            }
        )


def _category_priority(category: str) -> int:
    return {
        "EMAIL": 0,
        "PHONE": 0,
        "DATE": 1,
        "IDENTIFIER": 1,
        "MANUAL": 0,
        "NER_ENTITY": 2,
        "CAPITALIZED_PHRASE": 3,
    }.get(category, 4)


def _priority_level(category: str, ner_labels: set[str], ner_score: float) -> int:
    if category != "NER_ENTITY":
        return CANDIDATE_PRIORITY_LEVELS.get(category, 8)
    label_priorities = [
        NER_LABEL_PRIORITY_LEVELS.get(label.upper(), 8)
        for label in ner_labels
    ]
    base_priority = min(label_priorities, default=8)
    confidence_adjustment = round((max(0.0, min(1.0, ner_score)) - 0.5) * 2)
    return max(2, min(10, base_priority - confidence_adjustment))


def _candidate_id(version_id: str, normalized_term: str) -> str:
    return hashlib.sha256(f"{version_id}\0{normalized_term}".encode()).hexdigest()[:24]


def _capitalized_phrase_matches(text: str) -> Iterable[tuple[int, int]]:
    window = deque(maxlen=4)
    for current in _CAPITALIZED_TOKEN.finditer(text):
        window.append(current)
        for size in range(2, len(window) + 1):
            phrase = list(window)[-size:]
            first = phrase[0]
            if first.group(0).casefold().rstrip(".") in _COMMON_SENTENCE_STARTERS:
                continue
            if all(not text[left.end() : right.start()].strip(" \t") for left, right in pairwise(phrase)):
                yield first.start(), phrase[-1].end()


def _overlaps_date(text: str, start: int, end: int) -> bool:
    return any(pattern.search(text, start, end) for pattern in (_MONTH_DATE, _NUMERIC_DATE))
