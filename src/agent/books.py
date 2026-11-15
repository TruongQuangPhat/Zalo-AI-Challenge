"""Shop books read straight from source tables by their column headers (industry rules).

Rows map the plain header to (raw value, cell); an exact repeat of a row counts once, as the
fact build does. Tables are read once per shop.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from src.data.catalog import BusinessContext
from src.data.normalization.values import ParseError, parse_money
from src.jsonio import ContractError
from src.semantics.rulebook import plain

ORDER_HEADERS = {"stt", "hd", "so hd", "ma don", "so don", "ma hoa don"}


def money(raw) -> Decimal:
    """Shop money tokens: 19.990.000đ, 19tr990, 20,1tr, 120k, 3000k, 1200000."""
    token = re.sub(r"\s*(?:vnd|vnđ|đ|₫)$", "", str(raw).strip().casefold())
    if re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", token) or re.fullmatch(
        r"-?\d{1,3}(?:,\d{3}){2,}", token
    ):
        token = re.sub(r"[.,]", "", token)
    for convention in (None, "decimal_comma", "decimal_dot"):
        try:
            return parse_money(token, convention)
        except (ParseError, InvalidOperation, ValueError):
            continue
    raise ContractError(f"phone_money_unparsed:{raw}")


_CACHE: dict[tuple, list] = {}


def rows(context: BusinessContext):
    """(headers, rows, epoch) of every source table, read once per shop."""
    key = (context.catalog["business_id"], id(context))
    if key not in _CACHE:
        if len(_CACHE) > 4:
            _CACHE.clear()
        _CACHE[key] = list(read_rows(context))
    return _CACHE[key]


def read_rows(context: BusinessContext):
    """(headers, rows, epoch); rows map plain header -> (raw, cell)."""
    for profile in context.catalog["tables"]:
        if profile.get("read_status") not in {"read", "empty"}:
            continue
        source = context.source_table(profile["source_id"])
        epoch = source.metadata.get("date_system")
        header, rows, seen = None, [], set()
        last_header = None
        if profile.get("structure_status") == "resolved" and profile.get("header_rows"):
            # The catalog already resolved the (possibly two-row) header.
            header = {c["position"]: plain(str(c["display_name"])) for c in profile["columns"]}
            last_header = max(profile["header_rows"])
        for row in source.iter_rows():
            cells = {p: c for p, c in row.cells.items() if c.raw_value not in {None, ""}}
            if last_header is not None and row.row is not None and row.row <= last_header:
                continue
            if header is None:
                names = {p: plain(str(c.raw_value)) for p, c in cells.items()}
                if len(names) >= 3 and len(set(names.values())) == len(names):
                    header = names
                continue
            values = {header[p]: (c.raw_value, c) for p, c in cells.items() if p in header}
            key = tuple(sorted((k, str(v[0])) for k, v in values.items()))
            # An exact repeat of an order row is one row (as the fact build keeps it); a book
            # not kept by order counts every row (rulebook "Bảng không ghi theo đơn").
            by_order = bool(set(header.values()) & ORDER_HEADERS)
            if values and not (by_order and key in seen):
                seen.add(key)
                rows.append(values)
        if header:
            yield set(header.values()), rows, epoch


def table(context, required: set[str]):
    found = [(r, epoch) for headers, r, epoch in rows(context) if required <= headers]
    return found
