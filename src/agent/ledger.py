"""Item lines of a sales ledger (rulebook "Bảng không ghi theo đơn": each row is one entry).

A ledger's prepared facts are sales events (date, status, amount, row role) that keep the
source row they came from. Item-level operators need the item and its base quantity: they
are read from that same row, in the columns the README defines (item name, quantity
converted to the base unit). Rows are shaped like ``rule_ops._lines`` rows, with the event
standing in for the order and the item name for the product key.
"""

from __future__ import annotations

import json
from contextlib import closing

from src.data.catalog import BusinessContext
from src.data.normalization.entities import source_fields
from src.data.normalization.relational import abbreviation_table
from src.data.normalization.stock import identity_text
from src.data.normalization.values import ParseError, parse_number, strip_unit
from src.jsonio import ContractError
from src.semantics.rulebook import plain

_CACHE: dict[str, list[tuple]] = {}


def ledger_lines(context: BusinessContext) -> list[tuple] | None:
    """All ledger entries as (event, day, amount, role, item key, item, None, base quantity,
    quantity, None, item group, None, None, status), or None for any other shape."""
    prepared = context.prepared
    if prepared is None or prepared.summary.get("shape") != "sales_ledger":
        return None
    cache_key = str(prepared.path)
    if cache_key in _CACHE:
        return _CACHE[cache_key]
    tables = {}
    for profile in context.catalog["tables"]:
        ref = profile["source_ref"]
        fields = source_fields(context, profile)
        if {"item", "base_quantity"} <= fields.keys():
            tables[(ref["file"], ref["table"])] = (profile, fields)
    rows: dict[tuple, tuple] = {}
    for (file, table), (profile, fields) in tables.items():
        for row in context.source_table(profile["source_id"]).iter_rows():

            def raw(key, row=row, fields=fields):
                cell = row.cells.get(fields[key]) if key in fields else None
                return None if cell is None or cell.raw_value in {None, ""} else cell.raw_value

            rows[(file, table, row.row)] = (
                raw("item"),
                raw("base_quantity"),
                raw("quantity"),
                raw("unit"),
            )
    # The shop's own abbreviation list names the full item ("Mì HHTCC"); names that differ
    # only in writing (case, spaces, accents) are one item, shown in its accented spelling.
    abbreviations = abbreviation_table(context)
    spellings: dict[str, dict[str, int]] = {}

    def full(item) -> str | None:
        if item is None:
            return None
        text = str(item).strip()
        return abbreviations.get(identity_text(text), text)

    def item_key(name: str) -> str:
        return plain(identity_text(name)).replace(" ", "")

    for item, *_ in rows.values():
        name = full(item)
        if name:
            counts = spellings.setdefault(item_key(name), {})
            counts[name] = counts.get(name, 0) + 1
    shown = {
        key: max(counts, key=lambda n: (n != plain(n), counts[n], n))
        for key, counts in spellings.items()
    }
    result = []
    with closing(prepared.connect()) as conn:
        groups = dict(conn.execute("SELECT event_key,item_group FROM event_groups"))
        events = conn.execute(
            "SELECT event_key,substr(occurred_at,1,10),status,net_amount,row_role,source_ref "
            "FROM sales_events"
        ).fetchall()
    for event, day, status, amount, role, source_ref in events:
        ref = json.loads(source_ref)
        found = rows.get((ref["file"], ref["table"], ref["row"]))
        if found is None:
            raise ContractError("ledger_row_unreadable")
        item, base, sold, unit = found
        try:
            base = str(parse_number(base)) if base is not None else None
        except ParseError as exc:
            raise ContractError("ledger_quantity_unreadable") from exc
        try:  # "1 gói": the selling quantity may carry its unit
            sold = str(parse_number(strip_unit(sold, unit))) if sold is not None else None
        except (ParseError, ContractError):
            sold = None
        name = full(item)
        key = item_key(name) if name else None
        result.append(
            (
                event,
                day,
                amount,
                role,
                key,
                shown.get(key) if key else None,
                None,
                base,
                sold,
                None,
                groups.get(event),
                None,
                None,
                status,
            )
        )
    # An item's base unit is the unit its rows carry when the sold quantity already equals
    # the base quantity; only one such unit proves it.
    seen: dict[str, set[str]] = {}
    for item, base, sold, unit in rows.values():
        if item is None or unit is None or base is None or sold is None:
            continue
        try:
            same = parse_number(base) == parse_number(strip_unit(sold, unit))
        except (ParseError, ContractError):
            continue
        if same and full(item):
            seen.setdefault(item_key(full(item)), set()).add(identity_text(str(unit)))
    units = {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}
    result = [(*r[:12], units.get(r[4]), r[13]) for r in result]
    _CACHE[cache_key] = result
    return result
