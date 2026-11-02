from __future__ import annotations

import csv
import hashlib
import sqlite3
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from src.data.ingestion import READERS, ReaderOptions
from src.data.ingestion.sqlite_reader import connect
from src.data.profiling import ProfileOptions, candidate_relations, profile_table
from src.jsonio import ContractError


def workbook(path: Path, sheets: list[tuple[str, str, str]], *, date1904=False, shared=None):
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with zipfile.ZipFile(path, "w") as archive:
        names = "".join(
            f'<sheet name="{escape(name)}" sheetId="{i}" r:id="r{i}" state="{state}"/>'
            for i, (name, state, _) in enumerate(sheets, 1)
        )
        archive.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{ns}" xmlns:r="{rel}">'
            f'<workbookPr date1904="{int(date1904)}"/><sheets>{names}</sheets></workbook>',
        )
        targets = "".join(
            f'<Relationship Id="r{i}" Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, len(sheets) + 1)
        )
        archive.writestr("xl/_rels/workbook.xml.rels", f"<Relationships>{targets}</Relationships>")
        archive.writestr(
            "xl/styles.xml",
            f'<styleSheet xmlns="{ns}"><numFmts>'
            '<numFmt numFmtId="164" formatCode="dd/mm/yyyy"/></numFmts>'
            '<cellXfs><xf numFmtId="164"/></cellXfs></styleSheet>',
        )
        for i, (_, _, body) in enumerate(sheets, 1):
            archive.writestr(
                f"xl/worksheets/sheet{i}.xml", f'<worksheet xmlns="{ns}">{body}</worksheet>'
            )
        if shared is not None:
            archive.writestr(
                "xl/sharedStrings.xml",
                f'<sst xmlns="{ns}">'
                + "".join(f"<si><t>{escape(value)}</t></si>" for value in shared)
                + "</sst>",
            )


def text_cell(address, value):
    return f'<c r="{address}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'


def read(path: Path, options=None):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return READERS[path.suffix](path.parent, path, "synthetic", digest, options or ReaderOptions())


@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|"])
def test_csv_preserves_quoted_records_bom_and_physical_lines(tmp_path, delimiter):
    path = tmp_path / "sales.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=delimiter, lineterminator="\r\n")
        writer.writerow(["order_id", "note", "amount"])
        writer.writerow(["00123", f"one{delimiter}two\nnext", "99999999999999.00000001"])
    path.write_bytes(path.read_bytes().removesuffix(b"\r\n"))
    before = path.read_bytes()
    table = read(path)[0]
    rows = list(table.iter_rows())
    assert rows[1].row == 2 and rows[1].line_end == 3
    assert rows[1].ordinal == 2
    assert rows[1].cells[1].raw_value == "00123"
    assert rows[1].cells[2].raw_value == f"one{delimiter}two\nnext"
    assert rows[1].cells[3].raw_value == "99999999999999.00000001"
    assert rows[1].reference(table, 3)["column"] == 3
    assert rows[1].identity(table) != rows[0].identity(table)
    assert path.read_bytes() == before


def test_csv_width_duplicate_headers_empty_records_and_no_silent_truncation(tmp_path):
    path = tmp_path / "sales.csv"
    path.write_text("order_id,amount,amount\n001,2,3,EXTRA\n\n002,4\n")
    table = read(path, ReaderOptions(delimiter=","))[0]
    result, _ = profile_table(table, ProfileOptions())
    assert result["raw_row_count"] == 4
    assert result["row_accounting"] == {"blank": 1, "data_candidate": 2, "header": 1}
    assert result["structure_status"] == "ambiguous"
    assert result["issue_counts"]["record_width_mismatch"] == 2
    assert result["issue_counts"]["duplicate_header"] == 1
    assert len(result["columns"]) == 4
    assert len({c["column_id"] for c in result["columns"]}) == 4
    assert result["columns"][3]["samples"][0]["raw_value"] == "EXTRA"


def test_csv_encoding_and_malformed_quotes_require_evidence(tmp_path):
    path = tmp_path / "sales.csv"
    path.write_bytes("id;name\n01;café\n".encode("cp1252"))
    with pytest.raises(UnicodeError):
        read(path)
    table = read(path, ReaderOptions(encoding="cp1252"))[0]
    assert list(table.iter_rows())[1].cells[2].raw_value == "café"
    path.write_text('id,amount\n1,"unfinished\n')
    with pytest.raises(ContractError, match="CSV physical line"):
        list(read(path, ReaderOptions(delimiter=","))[0].iter_rows())
    path.write_text("ambiguous\nsource\n")
    with pytest.raises(ContractError, match="dialect"):
        read(path)


@pytest.mark.parametrize("date1904", [False, True])
def test_excel_native_cells_formula_cache_hidden_empty_and_raw_precision(tmp_path, date1904):
    path = tmp_path / "export.xlsx"
    header = '<row r="1">' + text_cell("A1", "order_id") + text_cell("B1", "amount") + "</row>"
    data = (
        '<row r="2"><c r="A2" t="s"><v>0</v></c>'
        '<c r="B2"><v>99999999999999.00000001</v></c>'
        '<c r="C2" s="0"><v>45658</v></c><c r="D2" t="b"><v>1</v></c>'
        '<c r="E2" t="e"><v>#VALUE!</v></c><c r="F2"><f>SUM(B2)</f></c>'
        '<c r="G2"><f>1+1</f><v>2</v></c><c r="I2"/></row>'
    )
    workbook(
        path,
        [
            (
                "T01_2025",
                "visible",
                f"<sheetData>{header}{data}</sheetData>"
                '<mergeCells><mergeCell ref="A1:A2"/></mergeCells>',
            ),
            ("hidden", "hidden", "<sheetData/>"),
            ("empty", "visible", "<sheetData/>"),
        ],
        date1904=date1904,
        shared=["00123"],
    )
    tables = read(path)
    assert len(tables) == 3
    rows = list(tables[0].iter_rows())
    cells = rows[1].cells
    assert cells[1].raw_value == "00123"
    assert cells[2].raw_value == "99999999999999.00000001"
    assert cells[3].raw_value == "45658" and cells[3].metadata["style"] == "0"
    assert cells[4].raw_value is True and cells[4].native_type == "boolean"
    assert cells[5].native_type == "error"
    assert cells[6].metadata["cache_status"] == "missing"
    assert cells[7].metadata["cache_status"] == "present" and cells[7].raw_value == "2"
    assert 8 not in cells and cells[9].native_type == "blank"
    assert tables[0].metadata["date_system"] == ("1904" if date1904 else "1900")
    assert tables[0].metadata["merged_ranges"] == ["A1:A2"]
    profile, _ = profile_table(tables[0], ProfileOptions())
    assert profile["structure_status"] == "ambiguous"
    assert profile["issue_counts"]["formula_cache_missing"] == 1
    assert profile["issue_counts"]["native_error"] == 1
    assert profile_table(tables[1], ProfileOptions())[0]["read_status"] == "empty"
    assert tables[1].metadata["state"] == "hidden"


def test_excel_preserves_group_blanks_and_summary_with_multirow_candidates(tmp_path):
    path = tmp_path / "grouped.xlsx"
    rows = (
        '<row r="1">'
        + text_cell("A1", "Export title")
        + '</row><row r="2"/><row r="3">'
        + text_cell("A3", "order_id")
        + text_cell("B3", "amount")
        + '</row><row r="4">'
        + text_cell("A4", "001")
        + '<c r="B4"><v>100</v></c></row><row r="5"><c r="B5"><v>50</v></c></row><row r="6">'
        + text_cell("A6", "Tổng ngày")
        + '<c r="B6"><v>150</v></c></row>'
    )
    workbook(path, [("sales", "visible", f"<sheetData>{rows}</sheetData>")])
    table = read(path)[0]
    raw = list(table.iter_rows())
    assert 1 not in raw[4].cells
    profile, _ = profile_table(table, ProfileOptions())
    assert profile["header_rows"] == [3]
    assert profile["raw_row_count"] == 6
    assert profile["row_accounting"]["summary_candidate"] == 1
    assert profile["columns"][0]["null_count"] == 1


def test_sqlite_read_only_storage_types_duplicate_lineage_and_without_rowid(tmp_path):
    path = tmp_path / "pos.sqlite"
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE "order detail" (id TEXT, amount, payload BLOB)')
        c.executemany(
            'INSERT INTO "order detail" VALUES (?,?,?)',
            [
                ("001", "1.600đ", b"\x00\xff"),
                ("001", "1.600đ", b"\x00\xff"),
                ("002", 100, None),
                ("003", 1.5, None),
            ],
        )
        c.execute("CREATE TABLE keyed (id TEXT PRIMARY KEY, amount INTEGER) WITHOUT ROWID")
        c.execute("INSERT INTO keyed VALUES ('A', 10)")
        c.execute("CREATE VIEW report AS SELECT * FROM keyed")
    before = path.read_bytes()
    tables = read(path)
    table = next(t for t in tables if t.source_ref["table"] == "order detail")
    rows = list(table.iter_rows())
    assert rows[0].identity(table) != rows[1].identity(table)
    assert rows[0].cells[1].raw_value == "001"
    assert rows[0].cells[2].native_type == "text"
    assert rows[2].cells[2].native_type == "integer"
    assert rows[3].cells[2].native_type == "real"
    assert rows[0].cells[3].raw_value == "AP8="
    assert rows[0].cells[3].metadata["encoding"] == "base64"
    keyed = next(t for t in tables if t.source_ref["table"] == "keyed")
    assert list(keyed.iter_rows())[0].locator == {
        "kind": "primary_key",
        "columns": ["id"],
        "values": ["A"],
    }
    assert (
        next(t for t in tables if t.source_ref["table"] == "report").metadata["exclusion_reason"]
        == "unsupported_view"
    )
    connection = connect(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE forbidden (id INTEGER)")
    finally:
        connection.close()
    assert path.read_bytes() == before


def test_profile_budgets_and_candidate_keys_do_not_claim_full_evidence(tmp_path):
    path = tmp_path / "orders.csv"
    path.write_text("order_id,amount\nA,1\nB,2\nC,3\n")
    profile, values = profile_table(read(path)[0], ProfileOptions(max_distinct=2))
    assert profile["read_status"] == "read"
    assert profile["raw_row_count"] == 4
    assert profile["statistics_status"] == "partial"
    assert profile["key_candidates"][0]["status"] == "not_checked"
    assert not values
    assert profile["coverage"]["available_span"] is None


def test_relationship_counts_detect_orphans_duplicates_and_fanout(tmp_path):
    parent = tmp_path / "orders.csv"
    child = tmp_path / "details.csv"
    parent.write_text("order_id,amount\nA,1\nA,1\nB,2\n")
    child.write_text("order_id,quantity\nA,1\nA,2\nX,1\n,1\n")
    profiles, values = [], {}
    for path in [parent, child]:
        profile, raw_keys = profile_table(read(path)[0], ProfileOptions())
        profiles.append(profile)
        values[profile["source_id"]] = raw_keys
    relation = next(
        r
        for r in candidate_relations(profiles, values)
        if r["parent"]["source_id"] == profiles[0]["source_id"]
    )
    assert relation["status"] == "candidate"
    assert relation["parent_unique"] is False
    assert relation["child_null_refs"] == 1
    assert (relation["matched_rows"], relation["unmatched_rows"]) == (2, 1)
    assert relation["max_parent_fanout"] == relation["max_child_fanout"] == 2


def test_different_tables_inside_header_lookahead_remain_ambiguous(tmp_path):
    path = tmp_path / "regions.xlsx"
    rows = (
        '<row r="1">' + text_cell("A1", "order_id") + text_cell("B1", "amount") + "</row>"
        '<row r="2">' + text_cell("A2", "A") + '<c r="B2"><v>100</v></c></row>'
        '<row r="3">'
        + text_cell("A3", "product_id")
        + text_cell("B3", "quantity")
        + text_cell("C3", "price")
        + "</row>"
        '<row r="4">' + text_cell("A4", "P") + '<c r="B4"><v>2</v></c><c r="C4"><v>30</v></c></row>'
    )
    workbook(path, [("mixed", "visible", f"<sheetData>{rows}</sheetData>")])
    table = read(path)[0]
    raw = list(table.iter_rows())
    assert len(raw) == 4 and raw[1].cells[1].raw_value == "A"
    profile, _ = profile_table(table, ProfileOptions())
    assert profile["structure_status"] == "ambiguous"
    assert sum(profile["row_accounting"].values()) == 4
