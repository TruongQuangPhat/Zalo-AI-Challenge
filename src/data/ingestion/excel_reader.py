"""Read OOXML cells without coercing numeric tokens to binary floats."""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator
from pathlib import Path

from src.data.ingestion.source import (
    Cell,
    ReaderOptions,
    SourceRow,
    SourceTable,
    source_reference,
)
from src.jsonio import ContractError

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _text(element: ET.Element) -> str:
    # Phonetic annotations are not part of the displayed rich-text value.
    return "".join(
        node.text or ""
        for node in element.findall(NS + "t") + element.findall(NS + "r/" + NS + "t")
    )


def column_position(address: str) -> int:
    match = re.fullmatch(r"([A-Z]+)[1-9]\d*", address)
    if not match:
        raise ContractError(f"invalid Excel cell address: {address}")
    value = 0
    for char in match[1]:
        value = value * 26 + ord(char) - 64
    return value


def _strings(archive: zipfile.ZipFile, budget: int) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    result, used = [], 0
    with archive.open("xl/sharedStrings.xml") as handle:
        context = ET.iterparse(handle, events=("start", "end"))
        _, root = next(context)
        for event, element in context:
            if event == "end" and element.tag == NS + "si":
                value = _text(element)
                used += len(value.encode()) + 64
                if used > budget:
                    raise ContractError("shared-string memory budget exceeded")
                result.append(value)
                root.clear()
    return result


def _cell(element: ET.Element, strings: list[str], options: ReaderOptions) -> Cell:
    token = element.findtext(NS + "v")
    kind = element.get("t", "n")
    attrs = {"address": element.get("r"), "style": element.get("s")}
    formula = element.find(NS + "f")
    if kind == "s":
        if token is None:
            raise ContractError("shared-string cell has no index")
        index = int(token)
        if index < 0 or index >= len(strings):
            raise ContractError("shared-string index out of range")
        value, native = strings[index], "text"
    elif kind == "inlineStr":
        inline = element.find(NS + "is")
        value = _text(inline) if inline is not None else ""
        native = "text"
    elif kind == "b":
        if token not in {"0", "1"}:
            raise ContractError("invalid Excel boolean")
        value, native = token == "1", "boolean"
    elif kind in {"str", "e", "d"}:
        value, native = token, {"str": "text", "e": "error", "d": "date_text"}[kind]
    elif kind == "n":
        value, native = token, "numeric" if token is not None else "blank"
    else:
        raise ContractError(f"unsupported Excel cell type: {kind}")
    if value is not None and len(str(value).encode()) > options.max_cell_bytes:
        raise ContractError("cell byte budget exceeded")
    if formula is not None:
        attrs.update(
            formula=formula.text,
            formula_attributes=dict(formula.attrib),
            cached_type=native,
            cache_status="missing" if token is None else "error" if kind == "e" else "present",
        )
        native = "formula"
    return Cell(value, native, attrs)


def _rows(
    path: Path, target: str, strings: list[str], metadata: dict, options: ReaderOptions
) -> Iterator[SourceRow]:
    metadata["merged_ranges"] = []
    with zipfile.ZipFile(path) as archive, archive.open(target) as handle:
        sheet_data = None
        ordinal, previous = 0, 0
        for event, element in ET.iterparse(handle, events=("start", "end")):
            if event == "start":
                if element.tag == NS + "sheetData":
                    sheet_data = element
                continue
            if element.tag == NS + "mergeCell":
                metadata["merged_ranges"].append(element.get("ref"))
            elif element.tag == NS + "dimension":
                metadata["declared_dimension"] = element.get("ref")
            elif element.tag == NS + "row":
                number = int(element.get("r", "0"))
                if number <= previous:
                    raise ContractError("worksheet rows must have increasing physical coordinates")
                previous = number
                cells = {}
                for node in element.findall(NS + "c"):
                    address = node.get("r", "")
                    position = column_position(address)
                    if position > options.max_columns:
                        raise ContractError("worksheet column budget exceeded")
                    if int(re.search(r"\d+$", address)[0]) != number or position in cells:
                        raise ContractError("duplicate/mismatched cell coordinate")
                    cells[position] = _cell(node, strings, options)
                ordinal += 1
                yield SourceRow(ordinal, number, cells)
                if sheet_data is not None:
                    sheet_data.clear()


def excel_tables(
    root: Path, path: Path, business: str, digest: str, options: ReaderOptions
) -> list[SourceTable]:
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        properties = workbook.find(NS + "workbookPr")
        date_system = (
            "1904"
            if properties is not None and properties.get("date1904") in {"1", "true"}
            else "1900"
        )
        relationships = {
            item.get("Id"): item
            for item in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        }
        strings = _strings(archive, options.max_shared_string_bytes)
        styles = []
        if "xl/styles.xml" in archive.namelist():
            style_root = ET.fromstring(archive.read("xl/styles.xml"))
            custom = {
                item.get("numFmtId"): item.get("formatCode")
                for item in style_root.findall(NS + "numFmts/" + NS + "numFmt")
            }
            styles = [
                {
                    "num_fmt_id": item.get("numFmtId"),
                    "format_code": custom.get(item.get("numFmtId")),
                }
                for item in style_root.findall(NS + "cellXfs/" + NS + "xf")
            ]
        tables = []
        for index, sheet in enumerate(workbook.findall(NS + "sheets/" + NS + "sheet")):
            relation = relationships[sheet.get(REL)]
            if relation.get("TargetMode") == "External":
                raise ContractError("external worksheet is not an offline source")
            target = relation.get("Target", "")
            target = (
                target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
            )
            name = sheet.get("name", "")
            metadata = {
                "sheet_index": index,
                "state": sheet.get("state", "visible"),
                "date_system": date_system,
                "styles": styles,
                "year_hints": sorted(set(re.findall(r"(?<!\d)20\d{2}(?!\d)", name))),
                "declared_columns": [],
            }

            def rows(target=target, metadata=metadata):
                yield from _rows(path, target, strings, metadata, options)

            tables.append(
                SourceTable(
                    source_reference(root, path, business, digest, name), "xlsx", metadata, rows
                )
            )
        return tables
