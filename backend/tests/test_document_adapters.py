import io
import zipfile

import pytest
from defusedxml import ElementTree as SafeET

from backend.app.document_adapters import (
    DocumentAdapterError,
    parse_document,
    serialize_with_replacements,
)

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def make_xlsx(*, unsafe_name=None):
    parts = {
        "[Content_Types].xml": b"""<?xml version='1.0' encoding='UTF-8'?>
            <Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'/>""",
        "xl/workbook.xml": b"""<?xml version='1.0' encoding='UTF-8'?>
            <workbook xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
              xmlns:r='http://schemas.openxmlformats.org/officeDocument/2006/relationships'>
              <sheets><sheet name='Data' sheetId='1' r:id='rId1'/></sheets>
            </workbook>""",
        "xl/_rels/workbook.xml.rels": b"""<?xml version='1.0' encoding='UTF-8'?>
            <Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>
              <Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet'
                Target='worksheets/sheet1.xml'/>
            </Relationships>""",
        "xl/sharedStrings.xml": b"""<?xml version='1.0' encoding='UTF-8'?>
            <sst xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main' count='1' uniqueCount='1'>
              <si><r><rPr><b/></rPr><t>Alex</t></r><r><t xml:space='preserve'> Tan</t></r></si>
            </sst>""",
        "xl/worksheets/sheet1.xml": b"""<?xml version='1.0' encoding='UTF-8'?>
            <worksheet xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
              xmlns:mc='http://schemas.openxmlformats.org/markup-compatibility/2006'
              xmlns:x14ac='http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac'
              mc:Ignorable='x14ac'>
              <sheetData><row r='1'>
                <c r='A1' s='5' t='s'><v>0</v></c>
                <c r='B1' t='inlineStr'><is><r><t>Project </t></r><r><t>Cedar</t></r></is></c>
                <c r='C1' t='str'><f>1+1</f><v>Alex Tan</v></c>
                <c r='D1' t='str'><v>Literal [[T_123]] value</v></c>
              </row></sheetData><extLst><ext uri='synthetic'><x14ac:foo/></ext></extLst>
            </worksheet>""",
        "xl/styles.xml": b"<styles>synthetic style part</styles>",
        "xl/drawings/drawing1.xml": b"<drawing>unsupported text</drawing>",
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
        if unsafe_name:
            archive.writestr(unsafe_name, b"unsafe")
    return output.getvalue(), parts


def read_xlsx_part(data, name):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.read(name)


def test_text_adapter_preserves_utf8_bom_and_mixed_line_endings():
    source = b"\xef\xbb\xbfAlex Tan [[T_001]]\r\nProject Cedar\n"
    parsed = parse_document(source, ".md")

    output = serialize_with_replacements(parsed, {"Alex Tan": "[[T_001]]"})

    assert parsed.encoding == "utf-8-sig"
    assert parsed.line_endings == "mixed"
    assert parsed.to_public_dict()["existingPlaceholderLikeTextCount"] == 1
    assert output == b"\xef\xbb\xbf[[T_001]] [[T_001]]\r\nProject Cedar\n"
    assert source == b"\xef\xbb\xbfAlex Tan [[T_001]]\r\nProject Cedar\n"


@pytest.mark.parametrize(
    ("source", "encoding", "expected"),
    [
        ("Alex Tan\r\n".encode("utf-16-le"), "utf-16-le", "[[T_001]]\r\n".encode("utf-16-le")),
        (b"\xfe\xff" + "Alex Tan\r\n".encode("utf-16-be"), "utf-16-be-bom", b"\xfe\xff" + "[[T_001]]\r\n".encode("utf-16-be")),
    ],
)
def test_text_adapter_detects_and_round_trips_utf16(source, encoding, expected):
    parsed = parse_document(source, "txt")

    assert parsed.encoding == encoding
    assert serialize_with_replacements(parsed, {"Alex Tan": "[[T_001]]"}) == expected


@pytest.mark.parametrize("source", [b"\xff", b"\x00\xff\x00", b"\xff\xfeA"])
def test_text_adapter_rejects_malformed_or_ambiguous_encoding(source):
    with pytest.raises(DocumentAdapterError, match="encoding|UTF-16|UTF-8"):
        parse_document(source, ".txt")


def test_csv_adapter_preserves_semicolon_dialect_quotes_and_record_endings():
    source = b'Name;Notes\r\n"Alex";"mentions; Cedar"\r\n'
    parsed = parse_document(source, ".csv")

    output = serialize_with_replacements(parsed, {"Alex": "[[T_001]]", "Cedar": "[[T_002]]"})
    reparsed = parse_document(output, ".csv")

    assert parsed.rows == (("Name", "Notes"), ("Alex", "mentions; Cedar"))
    assert parsed.to_public_dict()["dialect"] == {
        "delimiter": ";",
        "quoteCharacter": '"',
        "lineEndings": "crlf",
    }
    assert b'"[[T_001]]"' in output
    assert b'"mentions; [[T_002]]"' in output
    assert output.endswith(b"\r\n")
    assert reparsed.rows == (("Name", "Notes"), ("[[T_001]]", "mentions; [[T_002]]"))
    assert source == b'Name;Notes\r\n"Alex";"mentions; Cedar"\r\n'


def test_csv_adapter_preserves_multiline_quoted_cells_and_adds_quotes_when_needed():
    source = b'Name,Notes\nAlex,"first line\r\nsecond, line"\n'
    parsed = parse_document(source, ".csv")
    output = serialize_with_replacements(parsed, {"Alex": "Alex, Jr."})

    assert output == b'Name,Notes\n"Alex, Jr.","first line\r\nsecond, line"\n'
    assert parse_document(output, ".csv").rows[1] == ("Alex, Jr.", "first line\r\nsecond, line")


def test_csv_adapter_preserves_blank_records():
    source = b"Name,Notes\r\n\r\nAlex,brief\r\n"
    parsed = parse_document(source, ".csv")

    assert parsed.rows == (("Name", "Notes"), (), ("Alex", "brief"))
    assert serialize_with_replacements(parsed, {"Alex": "[[T_001]]"}) == b"Name,Notes\r\n\r\n[[T_001]],brief\r\n"


@pytest.mark.parametrize("source", [b"single column\nnext line\n", b"a,b\n1,2,3\n"])
def test_csv_adapter_rejects_ambiguous_or_inconsistent_dialects(source):
    with pytest.raises(DocumentAdapterError, match="ambiguous|inconsistent"):
        parse_document(source, ".csv")


def test_xlsx_adapter_reads_literal_cells_and_replaces_shared_and_rich_text_only():
    source, original_parts = make_xlsx()
    parsed = parse_document(source, ".xlsx")

    assert parsed.sheets[0].name == "Data"
    assert [(cell.location, cell.text) for cell in parsed.sheets[0].cells] == [
        ("A1", "Alex Tan"),
        ("B1", "Project Cedar"),
        ("D1", "Literal [[T_123]] value"),
    ]
    assert parsed.to_public_dict()["existingPlaceholderLikeTextCount"] == 1
    assert any("drawings/drawing1.xml" in warning for warning in parsed.warnings)
    assert serialize_with_replacements(parsed, {"not present": "replacement"}) == source

    output = serialize_with_replacements(
        parsed,
        {"Alex Tan": "[[T_001]]", "Project Cedar": "[[T_002]]", "Alex": "[[T_003]]"},
    )
    rewritten = parse_document(output, ".xlsx")

    assert [(cell.location, cell.text) for cell in rewritten.sheets[0].cells] == [
        ("A1", "[[T_001]]"),
        ("B1", "[[T_002]]"),
        ("D1", "Literal [[T_123]] value"),
    ]
    worksheet = SafeET.fromstring(read_xlsx_part(output, "xl/worksheets/sheet1.xml"))
    worksheet_xml = read_xlsx_part(output, "xl/worksheets/sheet1.xml")
    assert b"xmlns:x14ac=" in worksheet_xml
    assert b"mc:Ignorable=\"x14ac\"" in worksheet_xml
    formula = worksheet.find(f".//{{{MAIN_NS}}}c[@r='C1']")
    assert formula.find(f"{{{MAIN_NS}}}f").text == "1+1"
    assert formula.find(f"{{{MAIN_NS}}}v").text == "Alex Tan"
    assert worksheet.find(f".//{{{MAIN_NS}}}c[@r='A1']").attrib["s"] == "5"
    assert read_xlsx_part(output, "xl/styles.xml") == original_parts["xl/styles.xml"]
    assert read_xlsx_part(output, "xl/_rels/workbook.xml.rels") == original_parts["xl/_rels/workbook.xml.rels"]
    assert read_xlsx_part(output, "xl/drawings/drawing1.xml") == original_parts["xl/drawings/drawing1.xml"]
    assert source != output
    assert read_xlsx_part(source, "xl/sharedStrings.xml") == original_parts["xl/sharedStrings.xml"]


def test_xlsx_adapter_rejects_path_traversal_and_external_entities():
    unsafe, _ = make_xlsx(unsafe_name="../outside.xml")
    with pytest.raises(DocumentAdapterError, match="unsafe"):
        parse_document(unsafe, ".xlsx")

    entity_source, _ = make_xlsx()
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(entity_source)) as original, zipfile.ZipFile(output, "w") as changed:
        for name in original.namelist():
            value = original.read(name)
            if name == "xl/workbook.xml":
                value = b"<!DOCTYPE workbook [<!ENTITY x SYSTEM 'file:///etc/passwd'>]><workbook>&x;</workbook>"
            changed.writestr(name, value)
    with pytest.raises(DocumentAdapterError, match="XML|unsafe"):
        parse_document(output.getvalue(), ".xlsx")
