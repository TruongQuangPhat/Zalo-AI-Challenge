"""Evidence-backed identities and business facts, extending the existing prepared store.

No question or labels enter preparation. Optional families fail independently: a
missing payment key cannot invalidate an otherwise checked sales revenue export.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import replace
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from typing import TYPE_CHECKING

from src.data.cache import fingerprint
from src.data.normalization.relational import LABELS, MEANINGS, MappedSource, data_rows, token
from src.data.normalization.sales import SalesMapping
from src.data.normalization.stock import identity_text
from src.data.normalization.values import (
    cell_value,
    consistent_grouping,
    number_evidence,
    parse_date,
    parse_datetime,
    parse_money,
    parse_number,
)
from src.jsonio import ContractError, dumps
from src.semantics.rulebook import plain

if TYPE_CHECKING:
    from src.data.catalog import BusinessContext

SCHEMA = """
CREATE TABLE entities (
 entity_key TEXT PRIMARY KEY, kind TEXT NOT NULL, source_code TEXT,
 name TEXT NOT NULL, aliases TEXT NOT NULL, base_unit TEXT, source_ref TEXT NOT NULL
) STRICT;
CREATE TABLE order_parties (
 order_key TEXT PRIMARY KEY REFERENCES orders(order_key),
 customer_key TEXT REFERENCES entities(entity_key), payable TEXT, payment_method TEXT,
 source_ref TEXT NOT NULL
) STRICT;
CREATE TABLE line_items (
 line_key TEXT PRIMARY KEY REFERENCES order_lines(line_key),
 product_key TEXT NOT NULL REFERENCES entities(entity_key),
 base_quantity TEXT, sold_quantity TEXT, unit_cost TEXT, source_ref TEXT NOT NULL
) STRICT;
CREATE TABLE payments (
 payment_key TEXT PRIMARY KEY, order_key TEXT NOT NULL REFERENCES orders(order_key),
 paid_at TEXT NOT NULL, amount TEXT NOT NULL, source_ref TEXT NOT NULL
) STRICT;
CREATE TABLE purchases (
 purchase_key TEXT PRIMARY KEY, purchased_at TEXT NOT NULL, status TEXT NOT NULL,
 amount TEXT NOT NULL, source_ref TEXT NOT NULL
) STRICT;
CREATE TABLE stock_balances (
 event_key TEXT PRIMARY KEY, product_key TEXT NOT NULL REFERENCES entities(entity_key),
 occurred_at TEXT NOT NULL, balance TEXT NOT NULL, source_ref TEXT NOT NULL
) STRICT;
CREATE TABLE lots (
 lot_id TEXT PRIMARY KEY, product_key TEXT NOT NULL REFERENCES entities(entity_key),
 purchase_id TEXT NOT NULL REFERENCES purchases(purchase_key), expiry TEXT NOT NULL,
 received_quantity TEXT NOT NULL, source_ref TEXT NOT NULL
) STRICT;
CREATE TABLE lot_allocations (
 allocation_key TEXT PRIMARY KEY, order_key TEXT NOT NULL REFERENCES orders(order_key),
 line_key TEXT NOT NULL REFERENCES line_items(line_key),
 lot_id TEXT NOT NULL REFERENCES lots(lot_id),
 quantity TEXT NOT NULL, returned_quantity TEXT NOT NULL, returned_at TEXT, source_ref TEXT NOT NULL
) STRICT;
CREATE INDEX line_items_product ON line_items(product_key);
CREATE INDEX payments_order_date ON payments(order_key,paid_at);
CREATE INDEX stock_product_date ON stock_balances(product_key,occurred_at);
CREATE INDEX lots_product ON lots(product_key);
CREATE INDEX lot_allocations_line ON lot_allocations(line_key);
"""

EXTRA_MEANINGS = {
    "snapshot_balance": {"ton kho la ton cuoi ngay cuoi co du lieu, theo don vi co ban"},
    "base_quantity": {"so luong da doi ra don vi co ban", "base quantity"},
    "base_unit": {"don vi co ban", "base unit"},
    "unit": {"don vi tinh", "selling unit"},
    "payable": {"tong tien don", "order payable"},
    "payment_method": {
        "hinh thuc thanh toan cua don: cash tien mat, transfer chuyen khoan, "
        "ewallet vi dien tu, cod giao hang thu tien, debt ghi no",
        "order payment method",
    },
    "payment_id": {"ma phieu thu", "payment id"},
    "paid_at": {"thoi diem thu", "payment date"},
    "paid_amount": {"so tien", "payment amount"},
}
EXTRA_LABELS = {
    "product_id": {"idsp"},
    "item": {"hanghoa"},
    "base_quantity": {"slquydoi", "soluongquydoi", "slcoban"},
    "base_unit": {"donvicoban", "donvicoso", "dvtcoban", "donvi"},
    "unit": {"donvitinh", "dvt", "dvi"},
    "payable": {"tongthanhtoan", "tongtien"},
    "payment_method": {"phuongthuctt", "hinhthucthanhtoan"},
    "payment_id": {"maphieuthu", "phieuthu"},
    "paid_at": {"ngaygiothu", "ngaythu", "thoigianthu"},
    "paid_amount": {"sotienthu"},
    "unit_cost": {"giavonlucban"},
    "purchase_id": {"maphieunhap"},
    "purchased_at": {"ngaynhapkho"},
    "purchase_amount": {"tonggiatrinhap"},
    "stock_event_id": {"magiaodichkho"},
    "stock_at": {"ngaygiophatsinh"},
    "balance": {"toncuoi"},
    "lot_id": {"malo"},
    "expiry": {"hansudung"},
    "received_quantity": {"slnhapquydoi"},
    "line_number": {"sothutu"},
    "returned_at": {"ngaygiotra"},
    "snapshot_balance": {"tonkho"},
}


def source_fields(context: BusinessContext, profile: dict) -> dict[str, int]:
    readme = context.catalog["readme"]
    sections = re.findall(
        r"^## Cột trong `([^`]+)`(?:, sheet `([^`]+)`)?\s*\n(.*?)(?=^## |\Z)",
        readme,
        re.M | re.S,
    )
    filename, sheet = Path(profile["source_ref"]["file"]).name, profile["source_ref"]["table"]
    bodies = [body for name, sh, body in sections if name == filename and (not sh or sh == sheet)]
    body = bodies[0] if len(bodies) == 1 else readme if not sections else ""
    definitions = {
        plain(k): plain(v) for k, v in re.findall(r"^\s*-\s+\*\*(.+?)\*\*\s*:\s*(.+)$", body, re.M)
    }
    pos = bool(re.search(r"\bpos\b|phan mem ban hang", plain(readme)))
    result = {}
    for key in set(MEANINGS) | set(EXTRA_MEANINGS) | set(EXTRA_LABELS):
        meanings = MEANINGS.get(key, set()) | EXTRA_MEANINGS.get(key, set())
        labels = LABELS.get(key, set()) | EXTRA_LABELS.get(key, set())
        candidates = [
            c["position"]
            for c in profile["columns"]
            if definitions.get(plain(c["display_name"])) in meanings
            or (
                pos
                and plain(c["display_name"]) not in definitions
                and token(c["display_name"]) in labels
            )
        ]
        if len(candidates) == 1:
            result[key] = candidates[0]
    return result


def build_entities(
    context: BusinessContext, conn: sqlite3.Connection, summary: dict, mapping
) -> dict:
    """Keep source codes distinct, attach details through checked existing lineage."""
    conn.executescript(SCHEMA)
    if summary.get("shape") == "inventory_snapshot":
        return {"status": "unsupported", "reason": "snapshot uses existing point lookup"}
    sources = []
    if isinstance(mapping, SalesMapping):
        for m in [mapping, *mapping.continuations]:
            p = next(p for p in context.catalog["tables"] if p["source_id"] == m.source_id)
            sources.append(MappedSource(p, {**source_fields(context, p), **m.fields}, "combined"))
    else:
        sources = [
            replace(s, fields={**source_fields(context, s.profile), **s.fields})
            for s in mapping.sources
        ]
    errors: dict[str, list[str]] = {}

    def fail(family, message):
        if message not in errors.setdefault(family, []):
            errors[family].append(message)

    namespace = {"split": context.catalog["split"], "business": context.catalog["business_id"]}
    masters = {"product": {}, "customer": {}}
    names = {"product": {}, "customer": {}}

    def entity(kind, code, name, unit, ref):
        code, name = str(code or "").strip(), str(name or "").strip()
        if not code and not name:
            raise ContractError("missing_entity_identity")
        if not code and kind == "customer" and plain(name) in {"khach le", "khach vang lai"}:
            return None
        raw = code or identity_text(name)
        key = fingerprint(
            {**namespace, "kind": kind, "id": raw, "basis": "code" if code else "name"}
        )
        old = conn.execute(
            "SELECT name, aliases, base_unit FROM entities WHERE entity_key=?", (key,)
        ).fetchone()
        from src.jsonio import loads

        aliases = loads(old[1]) if old else []
        if name and name not in aliases:
            aliases.append(name)
        if old and old[2] and unit and identity_text(old[2]) != identity_text(unit):
            raise ContractError("entity_base_unit_conflict")
        base_unit = (old[2] if old else None) or unit or None
        if old:
            # REPLACE deletes the parent row, needlessly checking every child FK
            # per sale. Keep identity stable and update only new alias evidence.
            if dumps(aliases) != old[1] or base_unit != old[2]:
                conn.execute(
                    "UPDATE entities SET aliases=?,base_unit=? WHERE entity_key=?",
                    (dumps(aliases), base_unit, key),
                )
        else:
            conn.execute(
                "INSERT INTO entities VALUES (?,?,?,?,?,?,?)",
                (key, kind, code or None, name or code, dumps(aliases), base_unit, dumps(ref)),
            )
        return key

    def get(row, fields, key):
        cell = row.cells.get(fields.get(key))
        return cell_value(cell) if cell and cell.raw_value not in {None, ""} else None

    date_columns = {}

    def timestamp(row, source, field):
        raw = get(row, source.fields, field)
        metadata = source.profile.get("metadata", {})
        epoch = metadata.get("date_system")
        policy = {"basis": "source_datetime_token"}
        if epoch and re.fullmatch(r"\d+(?:\.\d+)?", str(raw)):
            cell = row.cells[source.fields[field]]
            style = int(cell.metadata.get("style") or 0)
            styles = metadata.get("styles", [])
            code = styles[style].get("format_code") if style < len(styles) else None
            policy = {"basis": "workbook_epoch_exact", "date_system": epoch}
            if code and code.lower() in {
                "yyyy-mm-dd h:mm:ss",
                "yyyy-mm-dd hh:mm:ss",
                "dd/mm/yyyy h:mm:ss",
                "dd/mm/yyyy hh:mm:ss",
            }:
                number = Decimal(str(raw))
                whole = int(number)
                with localcontext() as arithmetic:
                    arithmetic.prec = len(str(raw)) + 20
                    seconds = ((number - whole) * 86400).quantize(
                        Decimal(1), rounding=ROUND_HALF_UP
                    )
                value = parse_datetime(str(whole), date_system=epoch) + timedelta(
                    seconds=int(seconds)
                )
                policy.update(
                    basis="workbook_epoch_explicit_whole_second_display",
                    format_code=code,
                    warning="Display precision removes sub-second noise; raw token retained.",
                )
            else:
                value = parse_datetime(raw, date_system=epoch)
        else:
            value = parse_datetime(raw)
        policies = date_columns.setdefault(source.profile["source_id"], {}).setdefault(field, [])
        if policy not in policies:
            policies.append(policy)
        return value.isoformat()

    money_columns = {}
    declared = [
        style
        for style, phrase in {
            "comma_grouped": "dau phay nhom nghin",
            "dot_grouped": "dau cham nhom nghin",
            "decimal_dot": "dau cham thap phan, khong nhom nghin",
            "decimal_comma": "dau phay thap phan, khong nhom nghin",
        }.items()
        if phrase in plain(context.catalog["readme"])
    ]
    for s in sources:
        columns = {
            k: []
            for k in ["payable", "unit_cost", "paid_amount", "purchase_amount"]
            if k in s.fields
        }
        if not columns:
            continue
        for _, row in data_rows(context, s):
            for field, values in columns.items():
                cell = row.cells.get(s.fields[field])
                if (
                    cell
                    and cell.raw_value not in {None, ""}
                    and cell.native_type not in {"numeric", "integer"}
                ):
                    values.append(str(cell_value(cell)))
        entries = {}
        for field, values in columns.items():
            evidence = {number_evidence(v) for v in values} - {None}
            invalid = (
                len(declared) > 1
                or len(evidence) > 1
                or bool(declared and evidence - set(declared))
            )
            convention = declared[0] if declared else next(iter(evidence), None)
            grouping = convention is None and bool(consistent_grouping(values))
            if grouping:
                convention = consistent_grouping(values)
            entries[field] = {
                "convention": "conflict" if invalid else convention,
                "basis": "user_2026_11_04_consistent_three_digit_groups"
                if grouping
                else "README_or_unambiguous_source_tokens",
                "warning": "Accepted export grouping; locale not proved." if grouping else None,
            }
        money_columns[s.profile["source_id"]] = entries

    def money(row, source, field):
        raw = get(row, source.fields, field)
        if raw is None:
            return None
        cell = row.cells[source.fields[field]]
        policy = money_columns[source.profile["source_id"]][field]
        if policy["convention"] == "conflict":
            raise ContractError("money_column_authority_or_format_conflict")
        return parse_money(
            raw,
            "decimal_dot" if cell.native_type in {"numeric", "integer"} else policy["convention"],
        )

    for s in sources:
        if s.role not in masters:
            continue
        kind = s.role
        for table, row in data_rows(context, s):
            f = s.fields
            code = get(row, f, kind + "_id")
            name = get(row, f, "item" if kind == "product" else "customer")
            try:
                key = entity(kind, code, name, get(row, f, "base_unit"), row.reference(table))
                masters[kind][str(code)] = key
                names[kind].setdefault(identity_text(str(name)), set()).add(key)
            except ContractError as exc:
                fail(kind, str(exc))
    orders = dict(conn.execute("SELECT raw_order_id, order_key FROM orders"))
    lineage = dict(conn.execute("SELECT raw_id, line_key FROM lineage WHERE line_key IS NOT NULL"))
    line_numbers = {}
    units = {}
    for s in sources:
        if s.role in {"combined", "detail"} and {"item", "unit"} <= s.fields.keys():
            for _, row in data_rows(context, s):
                n, u = get(row, s.fields, "item"), get(row, s.fields, "unit")
                if n and u:
                    units.setdefault(identity_text(str(n)), set()).add(str(u))
    # In code-less flat exports, only a single observed selling unit is a proved
    # base unit. A converted quantity may be ranked without claiming its unit.
    for s in sources:
        if s.role not in {"combined", "header", "detail"}:
            continue
        for table, row in data_rows(context, s):
            f, ref = s.fields, row.reference(table)
            oid = str(get(row, f, "order_id") or "")
            order = orders.get(oid)
            if not order:
                continue
            if s.role in {"combined", "header"}:
                try:
                    code, name = get(row, f, "customer_id"), get(row, f, "customer")
                    key = masters["customer"].get(str(code)) if code else None
                    if code and not key:
                        raise ContractError("orphan_customer_code")
                    if not code and name:
                        key = entity("customer", None, name, None, ref)
                    try:
                        payable = money(row, s, "payable") if "payable" in f else None
                    except ContractError as exc:
                        fail("debt", str(exc))
                        payable = None
                    payable = str(payable) if payable is not None else None
                    record = (order, key, payable, get(row, f, "payment_method"), dumps(ref))
                    old = conn.execute(
                        "SELECT customer_key,payable,payment_method FROM order_parties "
                        "WHERE order_key=?",
                        (order,),
                    ).fetchone()
                    if old and old != record[1:4]:
                        raise ContractError("order_party_conflict")
                    conn.execute("INSERT OR IGNORE INTO order_parties VALUES (?,?,?,?,?)", record)
                except ContractError as exc:
                    fail("customer", str(exc))
                    fail("debt", str(exc))
            line = lineage.get(row.identity(table))
            if not line:
                continue
            if "line_number" in f:
                marker = (order, str(get(row, f, "line_number")))
                if marker in line_numbers and line_numbers[marker] != line:
                    fail("lots", "order_line_number_conflict")
                line_numbers[marker] = line
            role = conn.execute(
                "SELECT row_role FROM order_lines WHERE line_key=?", (line,)
            ).fetchone()[0]
            if role not in {"sale_item", "return_item"}:
                continue
            try:
                code, name = get(row, f, "product_id"), get(row, f, "item")
                key = masters["product"].get(str(code)) if code else None
                if code and not key:
                    raise ContractError("orphan_product_code")
                if not code and masters["product"]:
                    candidates = names["product"].get(identity_text(str(name)), set())
                    if len(candidates) != 1:
                        raise ContractError("source_product_name_ambiguous_or_unknown")
                    key = next(iter(candidates))
                if key is None:
                    observed = units.get(identity_text(str(name)), set())
                    key = entity(
                        "product",
                        None,
                        name,
                        next(iter(observed)) if len(observed) == 1 else None,
                        ref,
                    )
                quantity, sold, cost = [
                    get(row, f, k) for k in ["base_quantity", "quantity", "unit_cost"]
                ]
                numbers = []
                for field, raw in zip(
                    ["base_quantity", "sold_quantity", "unit_cost"],
                    [quantity, sold, cost],
                    strict=True,
                ):
                    try:
                        if field == "unit_cost":
                            parsed = money(row, s, "unit_cost") if "unit_cost" in f else None
                            numbers.append(str(parsed) if parsed is not None else None)
                            continue
                        if field == "sold_quantity" and isinstance(raw, str):
                            unit = get(row, f, "unit")
                            match = re.fullmatch(r"([+-]?\d+(?:[.,]\d+)?)\s+(.+)", raw.strip())
                            if (
                                match
                                and unit
                                and identity_text(match[2]) == identity_text(str(unit))
                            ):
                                raw = match[1]
                        position = f.get("quantity" if field == "sold_quantity" else field)
                        cell = row.cells.get(position)
                        convention = (
                            "decimal_dot"
                            if cell and cell.native_type in {"numeric", "integer"}
                            else None
                        )
                        numbers.append(
                            str(parse_number(raw, convention)) if raw is not None else None
                        )
                    except ContractError as exc:
                        fail(field, str(exc))
                        numbers.append(None)
                conn.execute(
                    "INSERT OR IGNORE INTO line_items VALUES (?,?,?,?,?,?)",
                    (line, key, *numbers, dumps(ref)),
                )
            except ContractError as exc:
                fail("product", str(exc))
    for family, required in [
        ("payments", {"payment_id", "order_id", "paid_at", "paid_amount"}),
        ("purchases", {"purchase_id", "purchased_at", "status", "purchase_amount"}),
        ("stock", {"stock_event_id", "product_id", "stock_at", "balance"}),
    ]:
        matched = [s for s in sources if required <= s.fields.keys()]
        if not matched:
            fail(family, "source_mapping_unsupported")
            continue
        conn.execute("SAVEPOINT optional_family")
        try:
            for s in matched:
                rows = data_rows(context, s)
                numeric = (
                    "paid_amount"
                    if family == "payments"
                    else "purchase_amount"
                    if family == "purchases"
                    else "balance"
                )
                tokens = [
                    str(get(row, s.fields, numeric))
                    for _, row in rows
                    if get(row, s.fields, numeric) is not None
                ]
                convention = consistent_grouping(tokens) or "decimal_dot"
                count = 0
                for table, row in data_rows(context, s):
                    count += 1
                    f, ref = s.fields, dumps(row.reference(table))
                    amount = (
                        money(row, s, numeric)
                        if family != "stock"
                        else parse_number(get(row, f, numeric), convention)
                    )
                    if amount is None:
                        raise ContractError("business_amount_missing")
                    if family == "payments":
                        oid = orders.get(str(get(row, f, "order_id")))
                        if not oid or amount < 0:
                            raise ContractError("payment_orphan_or_refund_policy_unsupported")
                        payment_id = get(row, f, "payment_id")
                        if payment_id is None or not str(payment_id).strip():
                            raise ContractError("payment_identity_missing")
                        record = (
                            str(payment_id),
                            oid,
                            timestamp(row, s, "paid_at"),
                            str(amount),
                            ref,
                        )
                        conn.execute("INSERT INTO payments VALUES (?,?,?,?,?)", record)
                    elif family == "purchases":
                        status = str(get(row, f, "status"))
                        if plain(status) not in {"received", "cancelled", "da nhap", "huy"}:
                            raise ContractError("purchase_status_unsupported")
                        conn.execute(
                            "INSERT INTO purchases VALUES (?,?,?,?,?)",
                            (
                                str(get(row, f, "purchase_id")),
                                timestamp(row, s, "purchased_at"),
                                status,
                                str(amount),
                                ref,
                            ),
                        )
                    else:
                        product = masters["product"].get(str(get(row, f, "product_id")))
                        if product is None:
                            raise ContractError("stock_orphan_product")
                        conn.execute(
                            "INSERT INTO stock_balances VALUES (?,?,?,?,?)",
                            (
                                str(get(row, f, "stock_event_id")),
                                product,
                                timestamp(row, s, "stock_at"),
                                str(amount),
                                ref,
                            ),
                        )
            if not count:
                raise ContractError("empty_business_export_coverage_unknown")
        except (ContractError, sqlite3.IntegrityError) as exc:
            conn.execute("ROLLBACK TO optional_family")
            fail(family, str(exc))
        finally:
            conn.execute("RELEASE optional_family")
    snapshot_at = None
    snapshots = [
        s for s in sources if {"product_id", "base_unit", "snapshot_balance"} <= s.fields.keys()
    ]
    if snapshots:
        conn.execute("SAVEPOINT snapshot_family")
        try:
            if (
                len(snapshots) != 1
                or conn.execute("SELECT 1 FROM stock_balances LIMIT 1").fetchone()
            ):
                raise ContractError("stock_representations_require_reconciliation")
            text = plain(context.catalog["readme"])
            if "ton kho la ton cuoi ngay cuoi co du lieu, theo don vi co ban" not in text:
                raise ContractError("snapshot_base_unit_authority_missing")
            dates = re.findall(r"du lieu co den het (\d{1,2}/\d{1,2}/\d{4})", text)
            if len(set(dates)) != 1:
                raise ContractError("snapshot_end_date_missing_or_ambiguous")
            snapshot_at = parse_date(dates[0]).isoformat()
            source = snapshots[0]
            seen = set()
            for table, row in data_rows(context, source):
                product = masters["product"].get(str(get(row, source.fields, "product_id")))
                if product is None or product in seen:
                    raise ContractError("snapshot_product_key_or_cardinality_invalid")
                unit = str(get(row, source.fields, "base_unit") or "")
                previous = conn.execute(
                    "SELECT base_unit FROM entities WHERE entity_key=?", (product,)
                ).fetchone()[0]
                if not unit or (previous and identity_text(previous) != identity_text(unit)):
                    raise ContractError("snapshot_base_unit_conflict")
                conn.execute("UPDATE entities SET base_unit=? WHERE entity_key=?", (unit, product))
                quantity = parse_number(get(row, source.fields, "snapshot_balance"))
                conn.execute(
                    "INSERT INTO stock_balances VALUES (?,?,?,?,?)",
                    (
                        row.identity(table),
                        product,
                        snapshot_at,
                        str(quantity),
                        dumps(row.reference(table)),
                    ),
                )
                seen.add(product)
            if not seen:
                raise ContractError("empty_snapshot_unknown")
            errors.pop("stock", None)
        except (ContractError, sqlite3.IntegrityError) as exc:
            conn.execute("ROLLBACK TO snapshot_family")
            snapshot_at = None
            fail("stock", str(exc))
        finally:
            conn.execute("RELEASE snapshot_family")
    lot_sources = [
        s
        for s in sources
        if {"lot_id", "product_id", "purchase_id", "expiry", "received_quantity"} <= s.fields.keys()
    ]
    allocation_sources = [
        s
        for s in sources
        if {"lot_id", "order_id", "line_number", "base_quantity", "returned_quantity"}
        <= s.fields.keys()
    ]
    if lot_sources and allocation_sources and "purchases" not in errors and "lots" not in errors:
        conn.execute("SAVEPOINT lot_family")
        try:
            returns = {}
            for s in sources:
                if {"order_id", "returned_at"} <= s.fields.keys() and s.role == "return":
                    for _, row in data_rows(context, s):
                        key = orders.get(str(get(row, s.fields, "order_id")))
                        at = timestamp(row, s, "returned_at")
                        if not key or (key in returns and returns[key] != at):
                            raise ContractError("lot_return_date_missing_or_conflicting")
                        returns[key] = at
            for s in lot_sources:
                for table, row in data_rows(context, s):
                    f = s.fields
                    product = masters["product"].get(str(get(row, f, "product_id")))
                    quantity = parse_number(get(row, f, "received_quantity"), "decimal_dot")
                    if not product or quantity < 0:
                        raise ContractError("lot_product_or_quantity_invalid")
                    conn.execute(
                        "INSERT INTO lots VALUES (?,?,?,?,?,?)",
                        (
                            str(get(row, f, "lot_id")),
                            product,
                            str(get(row, f, "purchase_id")),
                            timestamp(row, s, "expiry")[:10],
                            str(quantity),
                            dumps(row.reference(table)),
                        ),
                    )
            for s in allocation_sources:
                for table, row in data_rows(context, s):
                    f = s.fields
                    order = orders.get(str(get(row, f, "order_id")))
                    line = line_numbers.get((order, str(get(row, f, "line_number"))))
                    lot = str(get(row, f, "lot_id"))
                    sold = parse_number(get(row, f, "base_quantity"), "decimal_dot")
                    returned = parse_number(get(row, f, "returned_quantity"), "decimal_dot")
                    product = conn.execute(
                        "SELECT product_key FROM lots WHERE lot_id=?", (lot,)
                    ).fetchone()
                    line_product = conn.execute(
                        "SELECT product_key FROM line_items WHERE line_key=?", (line,)
                    ).fetchone()
                    if (
                        product is None
                        or product != line_product
                        or not 0 <= returned <= sold
                        or (returned and order not in returns)
                    ):
                        raise ContractError("lot_allocation_key_quantity_or_return_invalid")
                    key = fingerprint({"order": order, "line": line, "lot": lot})
                    conn.execute(
                        "INSERT INTO lot_allocations VALUES (?,?,?,?,?,?,?,?)",
                        (
                            key,
                            order,
                            line,
                            lot,
                            str(sold),
                            str(returned),
                            returns.get(order),
                            dumps(row.reference(table)),
                        ),
                    )
            allocated = {}
            from src.data.normalization.values import exact_add

            for line, quantity in conn.execute("SELECT line_key,quantity FROM lot_allocations"):
                allocated[line] = exact_add(allocated.get(line, Decimal(0)), Decimal(quantity))
            for line, product, quantity in conn.execute(
                "SELECT i.line_key,i.product_key,i.base_quantity FROM line_items i "
                "JOIN order_lines l USING(line_key) JOIN orders o USING(order_key) "
                "WHERE o.status!='cancelled'"
            ):
                if conn.execute(
                    "SELECT 1 FROM lots WHERE product_key=? LIMIT 1", (product,)
                ).fetchone() and (quantity is None or allocated.get(line) != Decimal(quantity)):
                    raise ContractError("lot_allocations_do_not_reconcile_line_quantity")
        except (ContractError, sqlite3.IntegrityError) as exc:
            conn.execute("ROLLBACK TO lot_family")
            fail("lots", str(exc))
        finally:
            conn.execute("RELEASE lot_family")
    else:
        fail("lots", "lot_sources_keys_or_purchase_authority_unsupported")
    return {
        "status": "prepared",
        "stock_mode": "snapshot" if snapshot_at else "event_balance",
        "snapshot_at": snapshot_at,
        "money_columns": money_columns,
        "date_columns": date_columns,
        "issues": errors,
        "counts": {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in [
                "entities",
                "line_items",
                "order_parties",
                "payments",
                "purchases",
                "stock_balances",
                "lots",
                "lot_allocations",
            ]
        },
        "identity_policy": "source_codes_first; accent_preserving_exact_name_only; no_fuzzy",
    }
