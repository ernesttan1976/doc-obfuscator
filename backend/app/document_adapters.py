from __future__ import annotations

import bisect
import csv
import io
import posixpath
import re
import threading
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET
from xml.sax.saxutils import quoteattr

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

MAX_DOCUMENT_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_PREVIEW_CHARACTERS = 12_000
MAX_PREVIEW_CELLS = 2_000
MAX_PREVIEW_SHEETS = 100
PLACEHOLDER_LIKE_TEXT = re.compile(r"\[\[T_[A-Za-z0-9_-]{3,}\]\]", re.IGNORECASE)

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
WORD_TEXT_TAGS = {f"{{{WORD_NS}}}t", f"{{{WORD_NS}}}delText"}
OFFICE_TEXT_TAGS = {*WORD_TEXT_TAGS, f"{{{DRAWING_NS}}}t"}
OFFICE_PARAGRAPH_TAGS = {f"{{{WORD_NS}}}p", f"{{{DRAWING_NS}}}p"}
MAX_COVERAGE_PART_NAMES = 100
MAX_COVERAGE_PART_NAME_LENGTH = 240

ET.register_namespace("", MAIN_NS)
ET.register_namespace("r", DOC_REL_NS)
_XML_NAMESPACE_LOCK = threading.RLock()


class DocumentAdapterError(Exception):
    """A safe, user-actionable document parsing or serialization error."""


@dataclass(frozen=True)
class DocumentCell:
    location: str
    text: str


@dataclass(frozen=True)
class WorksheetContent:
    name: str
    cells: tuple[DocumentCell, ...]


@dataclass(frozen=True)
class CsvField:
    start: int
    end: int
    content_start: int
    content_end: int
    value: str
    quoted: bool
    prefix: str = ""


class _ConservativeCsvDialect(csv.Dialect):
    quotechar = '"'
    doublequote = True
    escapechar = None
    lineterminator = "\r\n"
    quoting = csv.QUOTE_MINIMAL
    skipinitialspace = False
    strict = True

    def __init__(self, delimiter: str) -> None:
        self.delimiter = delimiter


@dataclass
class ParsedDocument:
    format: str
    encoding: str
    source: bytes = field(repr=False)
    text: str | None = None
    rows: tuple[tuple[str, ...], ...] = ()
    sheets: tuple[WorksheetContent, ...] = ()
    warnings: tuple[str, ...] = ()
    examined_xml_part_count: int = 0
    examined_parts: tuple[str, ...] = ()
    skipped_parts: tuple[str, ...] = ()
    text_parts: tuple[str, ...] = ()
    unsupported_parts: tuple[str, ...] = ()
    line_endings: str | None = None
    preexisting_placeholder_count: int = 0
    _csv_fields: tuple[tuple[CsvField, ...], ...] = field(default=(), repr=False)
    _csv_dialect: csv.Dialect | None = field(default=None, repr=False)

    def to_public_dict(self) -> dict[str, object]:
        """Return a bounded preview and metadata without exposing source bytes."""
        result: dict[str, object] = {
            "format": self.format,
            "encoding": self.encoding,
            "warnings": list(self.warnings),
            "existingPlaceholderLikeTextCount": self.preexisting_placeholder_count,
        }
        if self.line_endings is not None:
            result["lineEndings"] = self.line_endings
        if self.format in {"TXT", "MD"}:
            assert self.text is not None
            result["text"] = self.text[:MAX_PREVIEW_CHARACTERS]
            result["truncated"] = len(self.text) > MAX_PREVIEW_CHARACTERS
        elif self.format == "CSV":
            assert self._csv_dialect is not None
            result["dialect"] = {
                "delimiter": self._csv_dialect.delimiter,
                "quoteCharacter": self._csv_dialect.quotechar,
                "lineEndings": self.line_endings,
            }
            rows, truncated = _csv_preview(self.rows)
            result["rows"] = rows
            result["truncated"] = truncated
        elif self.format == "XLSX":
            sheets, truncated = _xlsx_preview(self.sheets)
            result["sheets"] = sheets
            result["truncated"] = truncated
        else:
            assert self.text is not None
            result["text"] = self.text[:MAX_PREVIEW_CHARACTERS]
            result["truncated"] = len(self.text) > MAX_PREVIEW_CHARACTERS
            result["coverage"] = {
                "examinedXmlPartCount": self.examined_xml_part_count,
                "examinedXmlParts": _bounded_part_names(self.examined_parts),
                "skippedPartCount": len(self.skipped_parts),
                "skippedParts": _bounded_part_names(self.skipped_parts),
                "textPartCount": len(self.text_parts),
                "textParts": _bounded_part_names(self.text_parts),
                "unsupportedPartCount": len(self.unsupported_parts),
                "unsupportedParts": _bounded_part_names(self.unsupported_parts),
                "partNamesTruncated": (
                    len(self.examined_parts) > MAX_COVERAGE_PART_NAMES
                    or len(self.skipped_parts) > MAX_COVERAGE_PART_NAMES
                    or
                    len(self.text_parts) > MAX_COVERAGE_PART_NAMES
                    or len(self.unsupported_parts) > MAX_COVERAGE_PART_NAMES
                ),
            }
        return result


def _bounded_part_names(names: tuple[str, ...]) -> list[str]:
    return [name[:MAX_COVERAGE_PART_NAME_LENGTH] for name in names[:MAX_COVERAGE_PART_NAMES]]


def _csv_preview(rows: tuple[tuple[str, ...], ...]) -> tuple[list[list[dict[str, object]]], bool]:
    result = []
    used = 0
    cell_count = 0
    truncated = False
    for row_index, row in enumerate(rows, start=1):
        public_row = []
        for column_index, value in enumerate(row, start=1):
            if used >= MAX_PREVIEW_CHARACTERS or cell_count >= MAX_PREVIEW_CELLS:
                truncated = True
                break
            clipped = value[: MAX_PREVIEW_CHARACTERS - used]
            truncated = truncated or len(clipped) < len(value)
            public_row.append({"row": row_index, "column": column_index, "text": clipped})
            used += len(value)
            cell_count += 1
        result.append(public_row)
        if used >= MAX_PREVIEW_CHARACTERS or cell_count >= MAX_PREVIEW_CELLS:
            truncated = truncated or row_index < len(rows)
            break
    return result, truncated


def _xlsx_preview(
    worksheets: tuple[WorksheetContent, ...],
) -> tuple[list[dict[str, object]], bool]:
    result = []
    used = 0
    cell_count = 0
    truncated = False
    for sheet_index, sheet in enumerate(worksheets):
        if sheet_index >= MAX_PREVIEW_SHEETS:
            truncated = True
            break
        public_cells = []
        for cell in sheet.cells:
            if used >= MAX_PREVIEW_CHARACTERS or cell_count >= MAX_PREVIEW_CELLS:
                truncated = True
                break
            clipped = cell.text[: MAX_PREVIEW_CHARACTERS - used]
            truncated = truncated or len(clipped) < len(cell.text)
            public_cells.append({"address": cell.location, "text": clipped})
            used += len(cell.text)
            cell_count += 1
        result.append({"name": sheet.name, "cells": public_cells})
        if used >= MAX_PREVIEW_CHARACTERS or cell_count >= MAX_PREVIEW_CELLS:
            truncated = truncated or sheet_index < len(worksheets) - 1
            break
    return result, truncated


def parse_document(data: bytes, extension: str) -> ParsedDocument:
    if len(data) > MAX_DOCUMENT_BYTES:
        raise DocumentAdapterError("This document exceeds the 100 MB processing limit.")
    normalized_extension = extension.lower()
    if not normalized_extension.startswith("."):
        normalized_extension = f".{normalized_extension}"
    if normalized_extension in {".txt", ".md"}:
        text, encoding = _decode_text(data)
        return ParsedDocument(
            format=normalized_extension[1:].upper(),
            encoding=encoding,
            source=data,
            text=text,
            line_endings=_line_ending_style(text),
            preexisting_placeholder_count=_count_placeholder_like((text,)),
        )
    if normalized_extension == ".csv":
        text, encoding = _decode_text(data)
        rows, fields, dialect = _parse_csv(text)
        return ParsedDocument(
            format="CSV",
            encoding=encoding,
            source=data,
            text=text,
            rows=rows,
            _csv_fields=fields,
            _csv_dialect=dialect,
            line_endings=_line_ending_style(text),
            preexisting_placeholder_count=_count_placeholder_like(value for row in rows for value in row),
        )
    if normalized_extension == ".xlsx":
        return _parse_xlsx(data)
    if normalized_extension in {".docx", ".pptx"}:
        return _parse_office_document(data, normalized_extension)
    raise DocumentAdapterError("This document format is not supported by the local document adapters.")


def serialize_with_replacements(parsed: ParsedDocument, replacements: dict[str, str]) -> bytes:
    """Replace exact source strings while preserving their original format."""
    transform = _replacement_function(replacements)
    if not replacements:
        return parsed.source
    if parsed.format in {"TXT", "MD"}:
        assert parsed.text is not None
        return _encode_text(transform(parsed.text), parsed.encoding)
    if parsed.format == "CSV":
        return _serialize_csv(parsed, transform)
    if parsed.format == "XLSX":
        return _serialize_xlsx(parsed.source, transform)
    if parsed.format in {"DOCX", "PPTX"}:
        return _serialize_office_document(parsed.source, parsed.format, transform)
    raise DocumentAdapterError("This document format cannot be serialized safely.")


class _ReplacementFunction:
    def __init__(self, replacements: dict[str, str]) -> None:
        if any(not isinstance(key, str) or not key for key in replacements):
            raise DocumentAdapterError("Replacement terms must be non-empty text.")
        if any(not isinstance(value, str) for value in replacements.values()):
            raise DocumentAdapterError("Replacement values must be text.")
        self.replacements = replacements
        self.pattern = (
            re.compile("|".join(re.escape(key) for key in sorted(replacements, key=len, reverse=True)))
            if replacements
            else None
        )

    def __call__(self, value: str) -> str:
        if self.pattern is None:
            return value
        return self.pattern.sub(lambda match: self.replacements[match.group(0)], value)

    def edits(self, value: str) -> list[tuple[int, int, str]]:
        if self.pattern is None:
            return []
        return [
            (match.start(), match.end(), self.replacements[match.group(0)])
            for match in self.pattern.finditer(value)
        ]


def _replacement_function(replacements: dict[str, str]) -> _ReplacementFunction:
    return _ReplacementFunction(replacements)


def _decode_text(data: bytes) -> tuple[str, str]:
    for marker, encoding, label in (
        (b"\xef\xbb\xbf", "utf-8", "utf-8-sig"),
        (b"\xff\xfe", "utf-16-le", "utf-16-le-bom"),
        (b"\xfe\xff", "utf-16-be", "utf-16-be-bom"),
    ):
        if data.startswith(marker):
            return _decode_known_text(data[len(marker) :], encoding, label)
    if b"\x00" in data:
        encoding = _detect_utf16_encoding(data)
        return _decode_known_text(data, encoding, encoding)
    try:
        return data.decode("utf-8", errors="strict"), "utf-8"
    except UnicodeDecodeError as exc:
        raise DocumentAdapterError("The document is not valid UTF-8 or detectable UTF-16 text.") from exc


def _detect_utf16_encoding(data: bytes) -> str:
    if len(data) % 2:
        raise DocumentAdapterError("The text encoding is ambiguous or malformed; use UTF-8 or detectable UTF-16.")
    even_zeros = sum(byte == 0 for byte in data[0::2]) / max(len(data[0::2]), 1)
    odd_zeros = sum(byte == 0 for byte in data[1::2]) / max(len(data[1::2]), 1)
    if odd_zeros >= 0.25 and even_zeros < 0.05:
        return "utf-16-le"
    if even_zeros >= 0.25 and odd_zeros < 0.05:
        return "utf-16-be"
    raise DocumentAdapterError("The text encoding is ambiguous or malformed; use UTF-8 or detectable UTF-16.")


def _decode_known_text(data: bytes, codec: str, label: str) -> tuple[str, str]:
    try:
        return data.decode(codec, errors="strict"), label
    except UnicodeDecodeError as exc:
        raise DocumentAdapterError(f"The {label.upper()} document contains malformed text.") from exc


def _encode_text(text: str, encoding: str) -> bytes:
    try:
        if encoding == "utf-8-sig":
            return b"\xef\xbb\xbf" + text.encode("utf-8", errors="strict")
        if encoding == "utf-16-le-bom":
            return b"\xff\xfe" + text.encode("utf-16-le", errors="strict")
        if encoding == "utf-16-be-bom":
            return b"\xfe\xff" + text.encode("utf-16-be", errors="strict")
        return text.encode(encoding, errors="strict")
    except UnicodeEncodeError as exc:
        raise DocumentAdapterError("A replacement cannot be represented in the document's original encoding.") from exc


def _line_ending_style(text: str) -> str:
    endings = re.findall(r"\r\n|\r|\n", text)
    distinct = set(endings)
    if not distinct:
        return "none"
    if len(distinct) > 1:
        return "mixed"
    return {"\r\n": "crlf", "\n": "lf", "\r": "cr"}[endings[0]]


def _count_placeholder_like(values: Iterable[str]) -> int:
    return sum(len(PLACEHOLDER_LIKE_TEXT.findall(value)) for value in values)


def _parse_csv(text: str) -> tuple[tuple[tuple[str, ...], ...], tuple[tuple[CsvField, ...], ...], csv.Dialect]:
    if not text:
        raise DocumentAdapterError("The CSV dialect is ambiguous because the document is empty.")
    try:
        try:
            dialect = csv.Sniffer().sniff(text[:65536], delimiters=",;\t|")
        except csv.Error:
            dialect = _fallback_csv_dialect(text)
        reader = csv.reader(io.StringIO(text, newline=""), dialect, strict=True)
        rows = tuple(tuple(row) for row in reader)
    except (csv.Error, UnicodeError) as exc:
        raise DocumentAdapterError("The CSV dialect is ambiguous or its quoting is malformed.") from exc
    populated_rows = [row for row in rows if row]
    if not populated_rows or len(populated_rows[0]) < 2:
        raise DocumentAdapterError("The CSV dialect is ambiguous; a consistent delimiter could not be identified.")
    width = len(populated_rows[0])
    if any(row and len(row) != width for row in rows):
        raise DocumentAdapterError("The CSV has inconsistent row widths and cannot be round-tripped safely.")
    fields = _scan_csv_fields(text, dialect)
    if len(fields) != len(rows) or any(
        (scanned_row and len(scanned_row) != width) or tuple(item.value for item in scanned_row) != rows[index]
        for index, scanned_row in enumerate(fields)
    ):
        raise DocumentAdapterError("The CSV quoting is ambiguous and cannot be preserved safely.")
    return rows, fields, dialect


def _fallback_csv_dialect(text: str) -> csv.Dialect:
    candidates = []
    for delimiter in ",;\t|":
        dialect = _ConservativeCsvDialect(delimiter)
        try:
            rows = tuple(tuple(row) for row in csv.reader(io.StringIO(text, newline=""), dialect, strict=True))
        except csv.Error:
            continue
        populated = [row for row in rows if row]
        if populated and len(populated[0]) >= 2:
            width = len(populated[0])
            if all(not row or len(row) == width for row in rows):
                candidates.append(dialect)
    if len(candidates) != 1:
        raise DocumentAdapterError("The CSV dialect is ambiguous; a consistent delimiter could not be identified.")
    return candidates[0]


def _scan_csv_fields(text: str, dialect: csv.Dialect) -> tuple[tuple[CsvField, ...], ...]:
    rows: list[tuple[CsvField, ...]] = []
    row_fields: list[CsvField] = []
    cursor = 0
    row_number = 1
    while cursor < len(text):
        if text[cursor] in "\r\n":
            cursor = _consume_csv_line_ending(text, cursor)
            rows.append(())
            row_number += 1
            continue
        field, cursor = _scan_csv_field(text, cursor, dialect, row_number)
        row_fields.append(field)
        if cursor < len(text) and text[cursor] == dialect.delimiter:
            cursor += 1
            if cursor == len(text):
                row_fields.append(CsvField(cursor, cursor, cursor, cursor, "", False))
                rows.append(tuple(row_fields))
                row_fields = []
            continue
        if cursor < len(text) and text[cursor] in "\r\n":
            cursor = _consume_csv_line_ending(text, cursor)
            rows.append(tuple(row_fields))
            row_fields = []
            row_number += 1
            continue
        if cursor != len(text):
            raise DocumentAdapterError(f"The CSV contains malformed data near row {row_number}.")
    if row_fields:
        rows.append(tuple(row_fields))
    return tuple(rows)


def _consume_csv_line_ending(text: str, cursor: int) -> int:
    return cursor + (2 if text.startswith("\r\n", cursor) else 1)


def _scan_csv_field(text: str, start: int, dialect: csv.Dialect, row_number: int) -> tuple[CsvField, int]:
    prefix_end = start
    if dialect.skipinitialspace:
        while prefix_end < len(text) and text[prefix_end] == " ":
            prefix_end += 1
    if dialect.quotechar and prefix_end < len(text) and text[prefix_end] == dialect.quotechar:
        return _scan_quoted_csv_field(text, start, prefix_end, dialect)
    return _scan_unquoted_csv_field(text, start, dialect, row_number)


def _scan_quoted_csv_field(
    text: str,
    start: int,
    quote_position: int,
    dialect: csv.Dialect,
) -> tuple[CsvField, int]:
    quotechar = dialect.quotechar
    content_start = quote_position + 1
    cursor = content_start
    value_chars: list[str] = []
    while cursor < len(text):
        char = text[cursor]
        if dialect.escapechar and char == dialect.escapechar:
            if cursor + 1 >= len(text):
                raise DocumentAdapterError("The CSV ends with an incomplete escape sequence.")
            value_chars.append(text[cursor + 1])
            cursor += 2
        elif char == quotechar:
            if dialect.doublequote and cursor + 1 < len(text) and text[cursor + 1] == quotechar:
                value_chars.append(quotechar)
                cursor += 2
            else:
                content_end = cursor
                cursor += 1
                break
        else:
            value_chars.append(char)
            cursor += 1
    else:
        raise DocumentAdapterError("The CSV contains an unterminated quoted field.")
    field = CsvField(
        start,
        cursor,
        content_start,
        content_end,
        "".join(value_chars),
        True,
        text[start:quote_position],
    )
    return field, cursor


def _scan_unquoted_csv_field(
    text: str,
    start: int,
    dialect: csv.Dialect,
    row_number: int,
) -> tuple[CsvField, int]:
    cursor = start
    while cursor < len(text) and text[cursor] not in {dialect.delimiter, "\r", "\n"}:
        if dialect.escapechar and text[cursor] == dialect.escapechar and cursor + 1 < len(text):
            cursor += 2
        else:
            cursor += 1
    raw_value = text[start:cursor]
    prefix_length = len(raw_value) - len(raw_value.lstrip(" ")) if dialect.skipinitialspace else 0
    value = raw_value[prefix_length:]
    if dialect.escapechar:
        value = re.sub(
            re.escape(dialect.escapechar) + r"(.)",
            lambda match: match.group(1),
            value,
            flags=re.DOTALL,
        )
    field = CsvField(start, cursor, start + prefix_length, cursor, value, False, raw_value[:prefix_length])
    if cursor < len(text) and text[cursor] not in {dialect.delimiter, "\r", "\n"}:
        raise DocumentAdapterError(f"The CSV contains malformed data near row {row_number}.")
    return field, cursor


def _serialize_csv(parsed: ParsedDocument, transform: Callable[[str], str]) -> bytes:
    assert parsed.text is not None and parsed._csv_dialect is not None
    edits: list[tuple[int, int, str]] = []
    dialect = parsed._csv_dialect
    for row in parsed._csv_fields:
        for csv_field in row:
            replaced = transform(csv_field.value)
            if replaced == csv_field.value:
                continue
            if csv_field.quoted:
                edits.append((csv_field.start, csv_field.end, csv_field.prefix + _quote_csv(replaced, dialect)))
            else:
                needs_quotes = (dialect.skipinitialspace and replaced.startswith(" ")) or any(
                    char in replaced for char in (dialect.delimiter, "\r", "\n", dialect.quotechar or '"')
                )
                if needs_quotes:
                    encoded = csv_field.prefix + _quote_csv(replaced, dialect)
                else:
                    encoded = csv_field.prefix + _escape_csv_unquoted(replaced, dialect)
                edits.append((csv_field.start, csv_field.end, encoded))
    output = parsed.text
    for start, end, replacement in reversed(edits):
        output = output[:start] + replacement + output[end:]
    return _encode_text(output, parsed.encoding)


def _quote_csv(value: str, dialect: csv.Dialect) -> str:
    quotechar = dialect.quotechar or '"'
    if dialect.escapechar:
        escaped = value.replace(dialect.escapechar, dialect.escapechar * 2)
        if dialect.doublequote:
            escaped = escaped.replace(quotechar, quotechar * 2)
        else:
            escaped = escaped.replace(quotechar, dialect.escapechar + quotechar)
    elif dialect.doublequote:
        escaped = value.replace(quotechar, quotechar * 2)
    elif quotechar in value:
        raise DocumentAdapterError("A CSV replacement contains a quote that this dialect cannot escape safely.")
    else:
        escaped = value
    return f"{quotechar}{escaped}{quotechar}"


def _escape_csv_unquoted(value: str, dialect: csv.Dialect) -> str:
    if not dialect.escapechar:
        return value
    return value.replace(dialect.escapechar, dialect.escapechar * 2)


def _parse_office_document(data: bytes, extension: str) -> ParsedDocument:
    main_part = "word/document.xml" if extension == ".docx" else "ppt/presentation.xml"
    label = extension[1:].upper()
    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
            names = _validate_archive(archive)
            if "[Content_Types].xml" not in names or main_part not in names:
                raise DocumentAdapterError(f"The {label} package is missing its main document part.")
            paragraph_groups: list[str] = []
            text_parts: list[str] = []
            unsupported_parts: set[str] = set()
            xml_parts = [info.filename for info in archive.infolist() if info.filename.lower().endswith(".xml")]
            for part_name in xml_parts:
                root = _parse_xml(archive.read(part_name))
                groups = _office_paragraph_groups(root)
                supported_nodes = {id(node) for group in groups for node in group}
                part_text = ["".join(node.text or "" for node in group) for group in groups]
                part_text = [text for text in part_text if text]
                if part_text:
                    paragraph_groups.extend(part_text)
                    text_parts.append(part_name)
                if _office_part_has_unhandled_text(root, supported_nodes, part_name):
                    unsupported_parts.add(part_name)

            for part_name in names:
                lower_name = part_name.lower()
                if lower_name.startswith((
                    "word/media/",
                    "ppt/media/",
                    "word/embeddings/",
                    "ppt/embeddings/",
                    "word/activex/",
                    "ppt/activex/",
                )) or "vbaproject" in lower_name:
                    unsupported_parts.add(part_name)

            warnings = [
                "Only WordprocessingML w:t/w:delText and DrawingML a:t inside paragraphs are processed.",
                "Images/OCR, macros, embedded binary content, external relationship targets, and document metadata are not processed.",
            ]
            warning_parts = sorted(unsupported_parts)
            warnings.extend(
                f"Unsupported or unhandled editable text may remain in package part: {part_name[:MAX_COVERAGE_PART_NAME_LENGTH]}"
                for part_name in warning_parts[:MAX_COVERAGE_PART_NAMES]
            )
            if len(warning_parts) > MAX_COVERAGE_PART_NAMES:
                warnings.append(
                    f"{len(warning_parts) - MAX_COVERAGE_PART_NAMES} additional unsupported package parts are listed in coverage metadata."
                )
            text = "\n".join(paragraph_groups)
            return ParsedDocument(
                format=label,
                encoding="xml-utf-8",
                source=data,
                text=text,
                warnings=tuple(warnings),
                examined_xml_part_count=len(xml_parts),
                examined_parts=tuple(xml_parts),
                skipped_parts=tuple(name for name in names if not name.lower().endswith(".xml")),
                text_parts=tuple(text_parts),
                unsupported_parts=tuple(sorted(unsupported_parts)),
                preexisting_placeholder_count=_count_placeholder_like(paragraph_groups),
            )
    except DocumentAdapterError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, DefusedXmlException, ValueError) as exc:
        raise DocumentAdapterError(f"The {label} package is malformed, unsafe, or cannot be read.") from exc


def _office_paragraph_groups(root) -> list[list[ET.Element]]:
    groups: list[list[ET.Element]] = []

    def visit(element, active_group: list[ET.Element] | None = None) -> None:
        if element.tag in OFFICE_PARAGRAPH_TAGS:
            active_group = []
            groups.append(active_group)
        if element.tag in OFFICE_TEXT_TAGS and active_group is not None:
            active_group.append(element)
        for child in element:
            visit(child, active_group)

    visit(root)
    return [group for group in groups if group]


def _office_part_has_unhandled_text(root, supported_nodes: set[int], part_name: str) -> bool:
    if part_name.startswith("docProps/"):
        return True
    if part_name.endswith(".rels") or part_name == "[Content_Types].xml":
        return False
    for element in root.iter():
        if (
            element.text
            and element.text.strip()
            and (element.tag not in OFFICE_TEXT_TAGS or id(element) not in supported_nodes)
        ):
            return True
        for attribute, value in element.attrib.items():
            local_name = attribute.rsplit("}", 1)[-1].lower()
            if local_name in {"descr", "title"} and value.strip():
                return True
    return False


def _serialize_office_document(
    source: bytes,
    document_format: str,
    transform: _ReplacementFunction,
) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(source), "r") as original:
            names = _validate_archive(original)
            main_part = "word/document.xml" if document_format == "DOCX" else "ppt/presentation.xml"
            if "[Content_Types].xml" not in names or main_part not in names:
                raise DocumentAdapterError(f"The {document_format} package is missing its main document part.")
            changed_parts = {}
            for part_name in names:
                if not part_name.lower().endswith(".xml") or part_name.lower().startswith("docprops/"):
                    continue
                data = original.read(part_name)
                root = _parse_xml(data)
                changed = False
                for group in _office_paragraph_groups(root):
                    changed = _replace_text_nodes(group, transform) or changed
                if changed:
                    changed_parts[part_name] = _serialize_xml(root, data)
            if not changed_parts:
                return source
            return _rewrite_xlsx_archive(original, changed_parts)
    except DocumentAdapterError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, DefusedXmlException, ValueError) as exc:
        raise DocumentAdapterError(
            f"The {document_format} package could not be rewritten without changing its package structure."
        ) from exc


def _parse_xlsx(data: bytes) -> ParsedDocument:
    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
            names = _validate_archive(archive)
            required = {"[Content_Types].xml", "xl/workbook.xml", "xl/_rels/workbook.xml.rels"}
            if not required.issubset(names):
                raise DocumentAdapterError("The XLSX package is missing required workbook parts.")
            sheet_parts = _workbook_sheet_parts(archive, names)
            shared_strings = _load_shared_strings(archive, names)
            sheets = tuple(
                _read_xlsx_worksheet(archive, sheet_name, part_name, shared_strings)
                for sheet_name, part_name in sheet_parts
            )
            warnings = _xlsx_coverage_warnings(names)
            return ParsedDocument(
                format="XLSX",
                encoding="xml-utf-8",
                source=data,
                sheets=sheets,
                warnings=warnings,
                preexisting_placeholder_count=_count_placeholder_like(
                    cell.text for sheet in sheets for cell in sheet.cells
                ),
            )
    except DocumentAdapterError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, DefusedXmlException, ValueError) as exc:
        raise DocumentAdapterError("The XLSX package is malformed, unsafe, or cannot be read.") from exc


def _workbook_sheet_parts(archive: zipfile.ZipFile, names: set[str]) -> list[tuple[str, str]]:
    workbook = _parse_xml(archive.read("xl/workbook.xml"))
    relationships = _parse_xml(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        relationship.attrib.get("Id", ""): relationship.attrib.get("Target", "")
        for relationship in relationships.findall(f"{{{PKG_REL_NS}}}Relationship")
        if relationship.attrib.get("TargetMode") != "External"
    }
    result = []
    for sheet in workbook.findall(f".//{{{MAIN_NS}}}sheet"):
        sheet_name = sheet.attrib.get("name", "Worksheet")
        relationship_id = sheet.attrib.get(f"{{{DOC_REL_NS}}}id", "")
        target = targets.get(relationship_id)
        if not target:
            raise DocumentAdapterError("The XLSX workbook contains a worksheet with a missing relationship.")
        part_name = _resolve_part_target("xl/workbook.xml", target)
        if part_name not in names or not part_name.startswith("xl/worksheets/"):
            raise DocumentAdapterError("The XLSX worksheet relationship points outside the workbook sheets.")
        result.append((sheet_name, part_name))
    return result


def _load_shared_strings(archive: zipfile.ZipFile, names: set[str]) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    root = _parse_xml(archive.read("xl/sharedStrings.xml"))
    return [_element_text(item) for item in root.findall(f"{{{MAIN_NS}}}si")]


def _read_xlsx_worksheet(
    archive: zipfile.ZipFile,
    sheet_name: str,
    part_name: str,
    shared_strings: list[str],
) -> WorksheetContent:
    root = _parse_xml(archive.read(part_name))
    cells = []
    for cell in root.findall(f".//{{{MAIN_NS}}}c"):
        parsed_cell = _read_xlsx_cell(cell, shared_strings)
        if parsed_cell is not None:
            cells.append(parsed_cell)
    return WorksheetContent(sheet_name, tuple(cells))


def _read_xlsx_cell(cell, shared_strings: list[str]) -> DocumentCell | None:
    if cell.find(f"{{{MAIN_NS}}}f") is not None:
        return None
    cell_type = cell.attrib.get("t", "n")
    if cell_type not in {"s", "inlineStr", "str"}:
        return None
    address = cell.attrib.get("r")
    if not address:
        raise DocumentAdapterError("An XLSX cell is missing its address and cannot be previewed safely.")
    if cell_type == "s":
        value = cell.find(f"{{{MAIN_NS}}}v")
        if value is None or value.text is None:
            return None
        try:
            shared_index = int(value.text)
            if shared_index < 0:
                raise IndexError
            text_value = shared_strings[shared_index]
        except (ValueError, IndexError) as exc:
            raise DocumentAdapterError("The XLSX shared-string table is malformed.") from exc
    elif cell_type == "inlineStr":
        inline = cell.find(f"{{{MAIN_NS}}}is")
        if inline is None:
            return None
        text_value = _element_text(inline)
    else:
        value = cell.find(f"{{{MAIN_NS}}}v")
        text_value = (value.text or "") if value is not None else ""
    return DocumentCell(address, text_value)


def _validate_archive(archive: zipfile.ZipFile) -> set[str]:
    members = archive.infolist()
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise DocumentAdapterError("The Office package contains too many archive parts.")
    names: set[str] = set()
    total_size = 0
    for info in members:
        name = info.filename
        path = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or path.is_absolute()
            or ".." in path.parts
            or name in names
            or info.flag_bits & 0x1
        ):
            raise DocumentAdapterError("The Office package contains an unsafe or duplicate archive part.")
        names.add(name)
        total_size += info.file_size
        if info.file_size > MAX_ARCHIVE_BYTES or total_size > MAX_ARCHIVE_BYTES:
            raise DocumentAdapterError("The expanded Office package exceeds the safe processing limit.")
        if info.file_size and info.file_size / max(info.compress_size, 1) > 1000:
            raise DocumentAdapterError("The Office package contains an unsafe compression ratio.")
    if archive.testzip() is not None:
        raise DocumentAdapterError("The Office package contains a damaged archive part.")
    return names


def _parse_xml(data: bytes):
    try:
        return SafeET.fromstring(data)
    except (DefusedXmlException, ET.ParseError, ValueError) as exc:
        raise DocumentAdapterError("The Office package contains malformed or unsafe XML.") from exc


def _serialize_xml(root, original_xml: bytes) -> bytes:
    with _XML_NAMESPACE_LOCK:
        namespace_map = {}
        for _, (prefix, uri) in SafeET.iterparse(io.BytesIO(original_xml), events=("start-ns",)):
            namespace_map[prefix] = uri
            try:
                ET.register_namespace(prefix, uri)
            except ValueError:
                continue
        serialized = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        required_prefixes = _markup_compatibility_prefixes(original_xml)
        missing_prefixes = [
            prefix
            for prefix in required_prefixes
            if prefix in namespace_map and not re.search(rb"\sxmlns:" + re.escape(prefix.encode()) + rb"=", serialized)
        ]
        if missing_prefixes:
            root_tag = re.search(rb"<(?:[A-Za-z_][\w.-]*:)?[A-Za-z_][\w.-]*", serialized)
            if root_tag is not None:
                declarations = b"".join(
                    b" xmlns:" + prefix.encode() + b"=" + quoteattr(namespace_map[prefix]).encode()
                    for prefix in missing_prefixes
                )
                serialized = serialized[: root_tag.end()] + declarations + serialized[root_tag.end() :]
        return serialized


def _markup_compatibility_prefixes(original_xml: bytes) -> set[str]:
    markup_compatibility_ns = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    namespace_attributes = {
        f"{{{markup_compatibility_ns}}}Ignorable",
        f"{{{markup_compatibility_ns}}}PreserveElements",
        f"{{{markup_compatibility_ns}}}PreserveAttributes",
        f"{{{markup_compatibility_ns}}}ProcessContent",
    }
    root = SafeET.fromstring(original_xml)
    prefixes = set()
    for element in root.iter():
        for attribute, value in element.attrib.items():
            if attribute in namespace_attributes:
                tokens = value.split()
                prefixes.update(token.split(":", 1)[0] for token in tokens)
            elif element.tag == f"{{{markup_compatibility_ns}}}Choice" and attribute.rsplit("}", 1)[-1] == "Requires":
                prefixes.update(value.split())
    return prefixes


def _resolve_part_target(source_part: str, target: str) -> str:
    if target.startswith("/"):
        resolved = posixpath.normpath(target.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))
    if resolved == ".." or resolved.startswith("../") or "\\" in resolved:
        raise DocumentAdapterError("The XLSX package contains an unsafe part relationship.")
    return resolved


def _element_text(element) -> str:
    return "".join(node.text or "" for node in element.findall(f".//{{{MAIN_NS}}}t"))


_UNSUPPORTED_TEXT_PART = re.compile(
    r"^xl/(?:comments|threadedComments|drawings|charts|externalLinks|pivot|queryTables|slicer|customXml|embeddings|media|persons|richData|ctrlProps|activeX|macrosheets|dialogSheets|vbaProject|metadata|connections)(?:/|\d|\.)",
    re.IGNORECASE,
)


def _xlsx_coverage_warnings(names: set[str]) -> tuple[str, ...]:
    detected = tuple(
        f"Unsupported text-bearing workbook part was not processed: {name}"
        for name in sorted(names)
        if _UNSUPPORTED_TEXT_PART.match(name)
    )
    return ("Only literal worksheet text values are scanned; formulas and workbook metadata are not.", *detected)


def _serialize_xlsx(source: bytes, transform: Callable[[str], str]) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(source), "r") as original:
            names = _validate_archive(original)
            required = {"[Content_Types].xml", "xl/workbook.xml", "xl/_rels/workbook.xml.rels"}
            if not required.issubset(names):
                raise DocumentAdapterError("The XLSX package is missing required workbook parts.")
            changed_parts = _modified_xlsx_parts(original, names, transform)
            if not changed_parts:
                return source
            return _rewrite_xlsx_archive(original, changed_parts)
    except DocumentAdapterError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, DefusedXmlException, ValueError) as exc:
        raise DocumentAdapterError("The XLSX package could not be rewritten without changing its structure.") from exc


def _modified_xlsx_parts(
    archive: zipfile.ZipFile,
    names: set[str],
    transform: _ReplacementFunction,
) -> dict[str, bytes]:
    changed_parts = {}
    referenced_shared: set[int] = set()
    for _, part_name in _workbook_sheet_parts(archive, names):
        data = archive.read(part_name)
        rewritten = _replace_worksheet_text(data, referenced_shared, transform)
        if rewritten is not None:
            changed_parts[part_name] = rewritten
    shared_name = "xl/sharedStrings.xml"
    if shared_name in names and referenced_shared:
        shared_data = archive.read(shared_name)
        rewritten_shared = _replace_shared_string_text(shared_data, referenced_shared, transform)
        if rewritten_shared is not None:
            changed_parts[shared_name] = rewritten_shared
    elif referenced_shared:
        raise DocumentAdapterError("The XLSX shared-string table is missing.")
    return changed_parts


def _replace_worksheet_text(
    data: bytes,
    referenced_shared: set[int],
    transform: _ReplacementFunction,
) -> bytes | None:
    root = _parse_xml(data)
    changed = False
    for cell in root.findall(f".//{{{MAIN_NS}}}c"):
        if cell.find(f"{{{MAIN_NS}}}f") is not None:
            continue
        cell_type = cell.attrib.get("t", "n")
        if cell_type == "s":
            _collect_shared_string_index(cell, referenced_shared)
        elif cell_type == "inlineStr":
            inline = cell.find(f"{{{MAIN_NS}}}is")
            if inline is not None:
                changed = _replace_element_text(inline, transform) or changed
        elif cell_type == "str":
            value = cell.find(f"{{{MAIN_NS}}}v")
            if value is not None:
                original_value = value.text or ""
                value.text = transform(original_value)
                changed = changed or value.text != original_value
    return _serialize_xml(root, data) if changed else None


def _collect_shared_string_index(cell, referenced_shared: set[int]) -> None:
    value = cell.find(f"{{{MAIN_NS}}}v")
    if value is None or value.text is None:
        return
    try:
        index = int(value.text)
        if index < 0:
            raise ValueError
        referenced_shared.add(index)
    except ValueError as exc:
        raise DocumentAdapterError("The XLSX shared-string table is malformed.") from exc


def _replace_shared_string_text(
    data: bytes,
    referenced_shared: set[int],
    transform: _ReplacementFunction,
) -> bytes | None:
    root = _parse_xml(data)
    items = root.findall(f"{{{MAIN_NS}}}si")
    changed = False
    for index in referenced_shared:
        if index >= len(items):
            raise DocumentAdapterError("The XLSX shared-string table is malformed.")
        changed = _replace_element_text(items[index], transform) or changed
    return _serialize_xml(root, data) if changed else None


def _rewrite_xlsx_archive(archive: zipfile.ZipFile, changed_parts: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as rewritten:
        for info in archive.infolist():
            part = changed_parts.get(info.filename)
            rewritten.writestr(info, part if part is not None else archive.read(info.filename))
    return output.getvalue()


def _replace_element_text(element, transform: _ReplacementFunction) -> bool:
    text_nodes = element.findall(f".//{{{MAIN_NS}}}t")
    return _replace_text_nodes(text_nodes, transform)


def _replace_text_nodes(text_nodes: list[ET.Element], transform: _ReplacementFunction) -> bool:
    if not text_nodes:
        return False
    original_parts = [node.text or "" for node in text_nodes]
    original_text = "".join(original_parts)
    replaced_text = transform(original_text)
    if replaced_text == original_text:
        return False

    # Keep existing rich-text runs: replacement text is assigned to the run
    # where it starts, including matches that cross run boundaries.
    boundaries = [0]
    for part in original_parts:
        boundaries.append(boundaries[-1] + len(part))
    updated_parts = [""] * len(text_nodes)

    def append_original_range(start: int, end: int) -> None:
        for index, (run_start, run_end) in enumerate(pairwise(boundaries)):
            overlap_start = max(start, run_start)
            overlap_end = min(end, run_end)
            if overlap_start < overlap_end:
                updated_parts[index] += original_text[overlap_start:overlap_end]

    cursor = 0
    for start, end, replacement in transform.edits(original_text):
        append_original_range(cursor, start)
        run_index = max(0, min(len(text_nodes) - 1, bisect.bisect_right(boundaries, start) - 1))
        updated_parts[run_index] += replacement
        cursor = end
    append_original_range(cursor, len(original_text))
    for node, value in zip(text_nodes, updated_parts, strict=True):
        node.text = value
        if value[:1].isspace() or value[-1:].isspace():
            node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    return True
