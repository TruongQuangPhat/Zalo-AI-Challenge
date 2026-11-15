"""Shop books read straight from source tables by their column headers (industry rules).

Rows map the plain header to (raw value, cell); an exact repeat of a row counts once, as the
fact build does. Tables are read once per shop.
"""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation
from pathlib import PurePath

from src.data.catalog import BusinessContext
from src.data.normalization.values import ParseError, parse_money
from src.jsonio import ContractError
from src.semantics.rulebook import plain

ORDER_HEADERS = {"stt", "hd", "so hd", "ma don", "so don", "ma hoa don"}


def money(raw) -> Decimal:
    """Shop money tokens: 19.990.000đ, 19tr990, 20,1tr, 120k, 3000k, 1200000."""
    token = re.sub(r"\s*(?:vnd|vnđ|đ|₫)$", "", str(raw).strip().casefold())
    if (
        re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", token)
        or re.fullmatch(r"-?\d{1,3}(?:,\d{3}){2,}", token)
        or re.fullmatch(r"-?\d{1,3}(?: \d{3})+", token)
    ):
        token = re.sub(r"[., ]", "", token)
    for convention in (None, "decimal_comma", "decimal_dot"):
        try:
            return parse_money(token, convention)
        except (ParseError, InvalidOperation, ValueError):
            continue
    raise ContractError(f"phone_money_unparsed:{raw}")


_CACHE: dict[tuple, list] = {}


def _profile_headers(profile) -> set[str] | None:
    """Plain column names the catalog resolved, or None when the header is unresolved."""
    if profile.get("structure_status") == "resolved" and profile.get("header_rows"):
        return {plain(str(c["display_name"])) for c in profile["columns"]}
    return None


class _Kind:
    """The part of a source cell the book readers use: its native type."""

    __slots__ = ("native_type",)

    def __init__(self, native_type):
        self.native_type = native_type


def _read(context: BusinessContext, profile, keep: frozenset[str] | None = None):
    """(headers, rows, epoch) of one table, cached per shop, table and kept columns."""
    key = (context.catalog["business_id"], id(context), profile["source_id"], keep)
    if key not in _CACHE:
        if len(_CACHE) > 64:
            _CACHE.clear()
        _CACHE[key] = _read_table(context, profile, keep)
    return _CACHE[key]


def rows(context: BusinessContext):
    """(headers, rows, epoch) of every readable source table."""
    found = []
    for profile in context.catalog["tables"]:
        if profile.get("read_status") in {"read", "empty"}:
            result = _read(context, profile)
            if result is not None:
                found.append(result)
    return found


def read_rows(context: BusinessContext):
    return rows(context)


def _read_table(context: BusinessContext, profile, keep: frozenset[str] | None = None):
    """(headers, rows, epoch); rows map plain header -> (raw value, cell kind). With
    ``keep``, a row holds only those columns (an exact repeat is still judged on all)."""
    source = context.source_table(profile["source_id"])
    epoch = source.metadata.get("date_system")
    header, found_rows, seen = None, [], set()
    last_header = None
    if _profile_headers(profile) is not None:
        # The catalog already resolved the (possibly two-row) header.
        header = {c["position"]: plain(str(c["display_name"])) for c in profile["columns"]}
        last_header = max(profile["header_rows"])
    by_order = None
    for row in source.iter_rows():
        cells = {p: c for p, c in row.cells.items() if c.raw_value not in {None, ""}}
        if last_header is not None and row.row is not None and row.row <= last_header:
            continue
        if header is None:
            names = {p: plain(str(c.raw_value)) for p, c in cells.items()}
            if len(names) >= 3 and len(set(names.values())) == len(names):
                header = names
            continue
        if by_order is None:
            by_order = bool(set(header.values()) & ORDER_HEADERS)
        # An exact repeat of an order row is one row (as the fact build keeps it); a book
        # not kept by order counts every row (rulebook "Bảng không ghi theo đơn").
        if by_order:
            digest = hashlib.blake2b(
                repr(
                    sorted((header[p], str(c.raw_value)) for p, c in cells.items() if p in header)
                ).encode(),
                digest_size=16,
            ).digest()
            if digest in seen:
                continue
            seen.add(digest)
        values = {
            header[p]: (c.raw_value, _Kind(c.native_type))
            for p, c in cells.items()
            if p in header and (keep is None or header[p] in keep)
        }
        if values:
            found_rows.append(values)
    return (set(header.values()), found_rows, epoch) if header else None


_SECTION = re.compile(r"^## Cột trong `([^`]+)`(?:, sheet `([^`]+)`)?")
_BULLET = re.compile(r"^\s*-\s+\*\*(.+?)\*\*\s*:\s*(.+)$")


def column_meanings(context: BusinessContext, profile) -> dict[str, str]:
    """Plain header -> plain README meaning for one table ("## Cột trong `file`[, sheet]")."""
    readme = context.catalog.get("readme") or ""
    name = PurePath(profile["source_ref"]["file"]).name
    sheet = profile["source_ref"].get("table")
    sections: dict[tuple[str, str | None], dict[str, str]] = {}
    current = None
    for line in readme.splitlines():
        if line.startswith("#"):
            match = _SECTION.match(line)
            current = (match[1], match[2]) if match else None
            if current:
                sections.setdefault(current, {})
        elif current and (bullet := _BULLET.match(line)):
            sections[current][plain(bullet[1])] = plain(bullet[2])
    return sections.get((name, sheet)) or sections.get((name, None)) or {}


def role_columns(context, profile, header: str, meaning: str) -> list[int]:
    """Positions of the columns whose plain header fully matches ``header`` or whose README
    meaning matches ``meaning``."""
    meanings = column_meanings(context, profile)
    found = []
    for column in profile.get("columns", []):
        name = plain(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(column["display_name"])))
        described = meanings.get(plain(str(column["display_name"])), "")
        if re.fullmatch(header, name) or (described and re.search(meaning, described)):
            found.append(column["position"])
    return found


def _renames(context, profile, aliases: dict[str, str]) -> dict[str, str]:
    """Actual plain header -> standard name, for standard names the table lacks: the one
    column whose README meaning matches the alias. A column claimed twice is not renamed."""
    headers = {plain(str(c["display_name"])) for c in profile.get("columns", [])}
    meanings = column_meanings(context, profile)
    claims: dict[str, list[str]] = {}
    for standard, pattern in aliases.items():
        if standard in headers:
            continue
        hits = [h for h in headers if h not in aliases and re.search(pattern, meanings.get(h, ""))]
        if len(hits) == 1:
            claims.setdefault(hits[0], []).append(standard)
    return {actual: names[0] for actual, names in claims.items() if len(names) == 1}


def table(
    context,
    required: set[str],
    keep: set[str] | None = None,
    aliases: dict[str, str] | None = None,
):
    """Rows of the tables holding every required column; other tables are never read.
    ``keep`` limits the stored columns (with the required ones) for large books.

    ``aliases`` maps a standard column name to a pattern over the README meaning of a
    column: a table without that header but with exactly one column so described has the
    column read under the standard name (a shop that heads "Giá thu" as "Giá mua lại")."""
    found = []
    for profile in context.catalog["tables"]:
        if profile.get("read_status") not in {"read", "empty"}:
            continue
        resolved = _profile_headers(profile) is not None
        renames = _renames(context, profile, aliases) if aliases and resolved else {}
        back = {standard: actual for actual, standard in renames.items()}
        need = {back.get(n, n) for n in required}
        kept = frozenset({back.get(n, n) for n in keep} | need) if keep is not None else None
        known = _profile_headers(profile)
        if known is not None and not need <= known:
            continue
        result = _read(context, profile, kept)
        if result is None or not need <= result[0]:
            continue
        rows = result[1]
        if renames:
            rows = [{renames.get(k, k): v for k, v in row.items()} for row in rows]
        found.append((rows, result[2]))
    return found
