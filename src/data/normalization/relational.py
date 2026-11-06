"""Prepare mapped order/header/detail exports with checked N:1 source joins.

This is preparation only: no question, date cutoff, metric answer or generated SQL.
README column meanings take precedence over a finite vocabulary of explicit POS
column labels. Ambiguous fields/keys and orphan rows fail closed.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from typing import TYPE_CHECKING

from src.data.cache import fingerprint
from src.data.ingestion import SourceRow
from src.data.normalization.sales import _sheet_year, _signature
from src.data.normalization.stock import identity_text
from src.data.normalization.values import (
    ParseError,
    cell_value,
    consistent_grouping,
    normalize_status,
    number_evidence,
    parse_date,
    parse_datetime,
    parse_money,
    serial_system,
)
from src.data.profiling import normalized
from src.jsonio import ContractError, dumps

if TYPE_CHECKING:
    from src.data.catalog import BusinessContext

# Meaning aliases are schema vocabulary, never business IDs or numeric answers.
MEANINGS = {
    "order_id": {"ma don", "ma hoa don", "order id"},
    "ordered_at": {"ngay ban", "ngay dat hang", "order date"},
    "status": {"trang thai don", "trang thai don hang", "order status"},
    "amount": {"thanh tien sau giam", "line net amount"},
    "item": {"ten hang", "ten san pham", "product name", "ten"},
    "product_id": {"ma hang", "ma san pham", "product id"},
    "customer_id": {"ma khach", "ma khach (trong neu khach le)", "customer id"},
    "customer": {"ten khach", "ten khach (khach le neu khong luu)", "customer name"},
    "item_group": {"nhom hang", "nhom san pham", "product group"},
    "customer_group": {"nhom khach", "customer group"},
    "quantity": {"so luong", "quantity"},
    "returned_quantity": {"so luong khach tra lai (don vi co ban); lon hon 0: don do co tra hang"},
    "channel": {"kenh ban", "sales channel"},
    "clock": {
        "thoi diem tao don",
        "ngay gio tao don",
        "order creation timestamp",
        "thoi diem dat don (ngay gio)",
        "gio dat don (dong tra hang: luc tra)",
    },
}
LABELS = {
    "order_id": {"mahoadon", "madonhang", "madon", "orderid", "magiaodich", "mabill"},
    "ordered_at": {
        "ngaylap",
        "thoigianlap",
        "thoigian",
        "ngayban",
        "ngaymathang",
        "ngaydat",
        "orderdate",
    },
    "status": {"tinhtrang", "trangthai", "trangthaidonhang", "status"},
    "amount": {"trigia", "thanhtien", "linenetamount"},
    "item": {"tenhang", "tenhanghoa", "tensanpham", "tengoi", "productname", "mon", "chitiethang"},
    "product_id": {"mahang", "mahanghoa", "masanpham", "sku", "productid", "mahh", "masp"},
    "customer_id": {"makh", "makhachhang", "customerid", "makhmua"},
    "customer": {"khachhang", "tenkhachhang", "customername", "tenkh"},
    "item_group": {
        "danhmuc",
        "nhomhang3cap",
        "nhomhanghoa",
        "nhomhang",
        "productgroup",
        "phanloai",
    },
    "customer_group": {"loaikhachhang", "nhomkhachhang", "customergroup", "hang"},
    "quantity": {"sl", "soluong", "soluongsanpham", "quantity", "slg"},
    "returned_quantity": {
        "soluongtradtcoban",
        "soluongtradvtcoban",
        "soluongtra",
        "sltraquydoi",
        "sltradtcoban",
        "sltradvtcoban",
    },
    "channel": {"kenhbanhang", "kenhban", "saleschannel", "nguondon"},
    "clock": {"thoigiantao", "thoigianlap", "ngaygiotao", "createdat", "giotaodon"},
}


def token(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalized(value))


@dataclass(frozen=True)
class MappedSource:
    profile: dict
    fields: dict[str, int]
    role: str
    auxiliary_family: str | None = None


@dataclass(frozen=True)
class RelationalMapping:
    sources: tuple[MappedSource, ...]
    ledger: bool = False


def _independent_delivery(profile: dict, definitions: dict, body: str) -> bool:
    """Only a declared nonfinancial delivery grain can be isolated from sales.

    Unknown/financial columns remain blocking; filenames never establish this family.
    This does not map delivery facts or certify delivery keys/coverage/operators.
    """
    meanings = [definitions.get(normalized(c["display_name"]).strip()) for c in profile["columns"]]
    required = {"order id", "delivery batch id", "delivery date", "delivered quantity"}
    allowed = required | {"product id", "product name", "delivery status", "delivery unit"}
    declarations = [
        normalized(line).strip().rstrip(".")
        for line in body.splitlines()
        if line.strip() and not re.fullmatch(r"\s*-\s+\*\*.+?\*\*\s*:\s*.+", line)
    ]
    return (
        declarations
        in (
            ["moi dong mot dot giao hang"],
            ["moi dong mot dot giao hang, khong phai mot don ban moi"],
        )
        and required <= set(meanings)
        and set(meanings) <= allowed
    )


def relational_mapping(context: BusinessContext) -> RelationalMapping:
    if not context.catalog["read_complete"]:
        raise ContractError("requires completely read sources")
    context.rules.require("dedup")
    readme = context.catalog["readme"]
    text = normalized(readme)
    sections, section_bodies = {}, {}
    for filename, sheet, body in re.findall(
        r"^## Cột trong `([^`]+)`(?:, sheet `([^`]+)`)?\s*\n(.*?)(?=^## |\Z)", readme, re.M | re.S
    ):
        definitions = {}
        for label, meaning in re.findall(r"^\s*-\s+\*\*(.+?)\*\*\s*:\s*(.+)$", body, re.M):
            label = normalized(label).strip()
            if label in definitions:
                raise ContractError("duplicate README field definition")
            definitions[label] = normalized(meaning).strip().rstrip(".")
        key = (filename, sheet)
        if key in sections:
            raise ContractError("duplicate README source section")
        sections[key] = definitions
        section_bodies[key] = body
    if "thong tin don chi ghi o dau" in text:
        raise ContractError("unrecognized inheritance declaration")
    ledger = "khong ghi ma don" in text or "khong co ma don" in text
    sources = []
    for p in context.catalog["tables"]:
        if p["read_status"] == "not_read":
            if p["metadata"].get("object_type") == "view":
                raise ContractError("view dependencies unverified")
            continue
        if p["structure_status"] != "resolved":
            raise ContractError("unresolved source structure")
        name = Path(p["source_ref"]["file"]).name
        table = p["source_ref"]["table"]
        definitions = sections.get((name, table), sections.get((name, ""), {}))
        fields = {}
        for field, meanings in MEANINGS.items():
            candidates = [
                c["position"]
                for c in p["columns"]
                if definitions.get(normalized(c["display_name"]).strip()) in meanings
            ]
            # POS headers are semantic evidence; do not override explicit README meanings.
            if not candidates and ("pos" in text or "phan mem ban hang" in text):
                candidates = [
                    c["position"]
                    for c in p["columns"]
                    if normalized(c["display_name"]).strip() not in definitions
                    and token(c["display_name"]) in LABELS[field]
                ]
            if len(candidates) > 1:
                if field == "ordered_at":
                    preferred = [
                        c["position"]
                        for c in p["columns"]
                        if c["position"] in candidates
                        and token(c["display_name"]) in {"thoigianlap", "thoigian"}
                    ]
                    if len(preferred) == 1:
                        candidates = preferred
                if len(candidates) > 1:
                    raise ContractError("ambiguous mapping: " + field)
            if candidates:
                fields[field] = candidates[0]
        if (
            "customer_id" in fields
            and "item" in fields
            and "product_id" not in fields
            and "order_id" not in fields
        ):
            fields["customer"] = fields.pop("item")
        if (
            token(name).startswith("units")
            and definitions
            and not any(v.startswith("he so: so don vi co ban") for v in definitions.values())
        ):
            raise ContractError("unclassified conversion source")
        role = None
        auxiliary_family = None
        body = section_bodies.get((name, table), section_bodies.get((name, ""), ""))
        if _independent_delivery(p, definitions, body):
            role, auxiliary_family = "auxiliary", "delivery_batches"
        elif "mot dot giao hang" in normalized(body):
            # An explicit but unresolved declaration must not fall through to the
            # older auxiliary filename vocabulary and hide financial overlap.
            raise ContractError("delivery_family_mapping_or_authority_unsupported")
        elif {"order_id", "ordered_at", "status"} <= fields.keys():
            role = "combined" if {"amount", "item"} <= fields.keys() else "header"
        elif {"order_id", "amount"} <= fields.keys() and ({"product_id", "item"} & fields.keys()):
            role = "detail"
        elif ledger and {"ordered_at", "status", "amount", "item"} <= fields.keys():
            role = "ledger"
        elif "order_id" in fields and "returned_quantity" in fields:
            role = "return_quantity"
        elif "order_id" in fields and (
            "tra" in token(table + name) or "return" in token(table + name)
        ):
            role = "return"
        elif any(
            v.startswith("he so: so don vi co ban") for v in definitions.values()
        ) or re.search(
            r"nhap|purchase|tonkho|stock|thekho|donvi|quydoi|conversion|thuoc|drug|nhacungcap|supplier|nhanvien|chinhanh|cash|soquy|thanhtoan|payment|phongban|phieugiao|congno|lichsugia|banggia|lohang|calam|chiphi|phieuthu",
            token(table + name),
        ):
            role = "auxiliary"
        elif {"product_id", "item"} <= fields.keys():
            role = "product"
        elif {"customer_id", "customer"} <= fields.keys():
            role = "customer"
        if role is None:
            raise ContractError("unclassified source: " + name + "/" + table)
        sources.append(MappedSource(p, fields, role, auxiliary_family))
    roles = {s.role for s in sources}
    if "combined" in roles and ({"header", "detail"} & roles):
        raise ContractError("overlapping sales representations require reconciliation")
    combined = [s for s in sources if s.role == "combined"]
    if len(combined) > 1:
        numbered_parts = {
            re.sub(r"_\d+$", "", Path(s.profile["source_ref"]["file"]).stem)
            for s in combined
            if re.search(r"_\d+$", Path(s.profile["source_ref"]["file"]).stem)
        }
        declared_parts = (
            len(numbered_parts) == 1
            and all(
                re.search(r"_\d+$", Path(s.profile["source_ref"]["file"]).stem) for s in combined
            )
            and "thong tin don lap lai o moi dong" in text
            and all((Path(s.profile["source_ref"]["file"]).name, "") in sections for s in combined)
        )
        if not declared_parts and not any(
            cue in text
            for cue in ["nhieu file", "nhieu phan", "moi thang mot sheet", "chia moi thang"]
        ):
            raise ContractError("multi-source continuation authority missing")
    if ledger and "ledger" in roles:
        if roles - {"ledger", "auxiliary", "product"}:
            raise ContractError("mixed ledger/order grain requires reconciliation")
    elif not ("combined" in roles or {"header", "detail"} <= roles):
        raise ContractError("no complete order header/detail mapping")
    return RelationalMapping(tuple(sources), ledger)


def data_rows(context: BusinessContext, source: MappedSource):
    table = context.source_table(source.profile["source_id"])
    header = None
    positions = set(source.fields.values())
    group = {}
    for row in table.iter_rows():
        if row.row in source.profile.get("header_rows", []):
            header = _signature(row.cells)
            group = {}
            continue
        if header and _signature(row.cells) == header:
            group = {}
            continue
        if (
            source.profile.get("header_rows")
            and row.row is not None
            and row.row < min(source.profile["header_rows"])
        ):
            continue
        if not any(c.raw_value not in {None, ""} for c in row.cells.values()):
            group = {}
            continue
        # Summary requires absent transaction identity/date/status, not a monetary value.
        identity = [
            row.cells.get(source.fields[k])
            for k in ["order_id", "ordered_at", "status"]
            if k in source.fields
        ]
        summary = any(
            re.match(r"^(tong|cong)\s+(cong|ngay|thang|ky)\b", normalized(str(c.raw_value)).strip())
            or (
                source.role in {"combined", "ledger"}
                and normalized(str(c.raw_value)).strip() in {"tong", "cong"}
                and value(row, source, "quantity") is None
            )
            for c in row.cells.values()
        )
        if summary and not any(c and c.raw_value not in {None, ""} for c in identity):
            group = {}
            continue
        annotation = any(
            normalized(str(c.raw_value)).strip().startswith("ghi chu:") for c in row.cells.values()
        )
        transaction_fields = [
            row.cells.get(source.fields[k])
            for k in ["order_id", "ordered_at", "status", "amount", "quantity"]
            if k in source.fields
        ]
        if annotation and not any(c and c.raw_value not in {None, ""} for c in transaction_fields):
            group = {}
            continue
        if not any(
            row.cells.get(i) and row.cells[i].raw_value not in {None, ""} for i in positions
        ):
            raise ContractError("unclassified nonempty row")
        if source.role == "combined":
            fields = ["order_id", "ordered_at", "status", "customer_id", "customer"]
            oid = value(row, source, "order_id")
            previous_id = group.get(source.fields["order_id"])
            if oid is not None and (previous_id is None or oid != previous_id.raw_value):
                group = {}
            # Only a contiguous, already identified group can supply omitted headers.
            # Never inherit across a new key, blank row, summary or repeated header.
            missing = [
                source.fields[k]
                for k in fields
                if k in source.fields and value(row, source, k) is None
            ]
            if group and missing:
                context.rules.require("header_inheritance")
                cells = dict(row.cells)
                for position in missing:
                    if position in group:
                        cells[position] = group[position]
                row = replace(row, cells=cells)
            group = {
                source.fields[k]: row.cells[source.fields[k]]
                for k in fields
                if k in source.fields
                and source.fields[k] in row.cells
                and row.cells[source.fields[k]].raw_value not in {None, ""}
            }
        yield table, row


def value(row: SourceRow, source: MappedSource, key: str, default=None):
    if key not in source.fields:
        return default
    c = row.cells.get(source.fields[key])
    if c is None or c.raw_value in {None, ""}:
        return default
    return cell_value(c)


def build_relational(
    context: BusinessContext, conn: sqlite3.Connection, mapping: RelationalMapping
) -> dict:
    counts = Counter()
    blocked = set()
    group_errors = set()
    joins = []

    def issue(code, ref, metrics):
        blocked.update(metrics)
        counts[code] += 1
        detail = {
            "code": code,
            "source_ref": ref,
            "raw_value": None,
            "affected_metrics": metrics,
            "severity": "blocking",
        }
        conn.execute(
            "INSERT OR IGNORE INTO issues VALUES (?,?)", (fingerprint(detail), dumps(detail))
        )

    primary = [s for s in mapping.sources if s.role in {"header", "combined", "ledger"}]
    # Establish money conventions from full exports, including summaries. Native numbers
    # retain their exact token; SQLite REAL remains a source precision limitation.
    declared = [
        style
        for style, phrase in {
            "comma_grouped": "dau phay nhom nghin",
            "dot_grouped": "dau cham nhom nghin",
            "decimal_dot": "dau cham thap phan, khong nhom nghin",
            "decimal_comma": "dau phay thap phan, khong nhom nghin",
        }.items()
        if phrase in normalized(context.catalog["readme"])
    ]
    if len(declared) > 1:
        raise ContractError("conflicting README money conventions")
    conventions = {}
    money_policy_sources = []
    for s in mapping.sources:
        if "amount" not in s.fields:
            continue
        table = context.source_table(s.profile["source_id"])
        evidence = set()
        tokens = []
        for r in table.iter_rows():
            cell = r.cells.get(s.fields["amount"])
            if cell and cell.native_type == "text":
                money_text = re.sub(
                    r"\s*(?:vnd|vnđ|đ|₫)$", "", str(cell.raw_value), flags=re.I
                ).strip()
                if cell.raw_value not in {None, ""} and not re.search(r"[A-Za-zÀ-ỹ]", money_text):
                    tokens.append(str(cell.raw_value))
                e = number_evidence(str(cell.raw_value))
                if e:
                    evidence.add(e)
        if len(evidence) > 1:
            raise ContractError("conflicting amount conventions")
        if declared and evidence - set(declared):
            raise ContractError("amount convention contradicts README")
        conventions[s.profile["source_id"]] = (
            declared[0] if declared else next(iter(evidence), None)
        )
        if conventions[s.profile["source_id"]] is None:
            conventions[s.profile["source_id"]] = consistent_grouping(tokens)
            if conventions[s.profile["source_id"]]:
                money_policy_sources.append(s.profile["source_id"])
    date_systems = {}
    for source in primary:
        table = context.source_table(source.profile["source_id"])
        system = table.metadata.get("date_system")
        if system is None:
            serials = [
                str(value(row, source, "ordered_at"))
                for _, row in data_rows(context, source)
                if re.fullmatch(r"\d+(?:\.\d+)?", str(value(row, source, "ordered_at")))
            ]
            system = serial_system(serials, context.catalog["coverage"]["available_end_hint"])
        date_systems[source.profile["source_id"]] = system
    products = {}
    customers = {}
    product_name_codes = {}
    product_alias_codes = {}
    name_policy_matches = Counter()
    for s in mapping.sources:
        if s.role not in {"product", "customer"}:
            continue
        target = products if s.role == "product" else customers
        keyfield = "product_id" if s.role == "product" else "customer_id"
        for table, row in data_rows(context, s):
            key = str(value(row, s, keyfield, ""))
            name = str(value(row, s, "item" if s.role == "product" else "customer", ""))
            group_field = "item_group" if s.role == "product" else "customer_group"
            try:
                group = value(row, s, group_field, "") if group_field in s.fields else None
            except ParseError as exc:
                issue(exc.code, row.reference(table), [group_field + "_share"])
                group = None
            if not key or not name:
                raise ContractError("missing master identity")
            record = (name, group, row.reference(table))
            if key in target and target[key][1] != record[1]:
                group_errors.add(
                    "item_group_share" if s.role == "product" else "customer_group_share"
                )
                target[key] = (target[key][0], None, target[key][2])
            elif key not in target:
                target[key] = record
            if s.role == "product":
                product_name_codes.setdefault(identity_text(name), set()).add(key)
                product_alias_codes.setdefault(token(name), set()).add(key)
        joins.append(
            {
                "kind": s.role,
                "source_id": s.profile["source_id"],
                "cardinality": "key-to-group checked after folding source name aliases",
                "keys": len(target),
            }
        )
    negative_returns = bool(
        re.search(
            r"so luong am la tra hang|hang tra ghi so luong am|"
            r"don co dong so luong am la don co tra hang",
            normalized(context.catalog["readme"]),
        )
    )
    headers = {}
    return_events = {}
    for s in primary:
        if s.role == "ledger":
            continue
        for table, row in data_rows(context, s):
            ref = row.reference(table)
            raw_id = row.identity(table)
            try:
                date_value = value(row, s, "ordered_at")
                cell = row.cells.get(s.fields["ordered_at"])
                dt = parse_datetime(
                    date_value,
                    year=_sheet_year(s.profile),
                    date_system=date_systems[s.profile["source_id"]]
                    if cell and re.fullmatch(r"\d+(?:\.\d+)?", str(cell.raw_value))
                    else None,
                )
                day = (
                    dt.isoformat()
                    if re.search(r"\d:\d", str(date_value))
                    else dt.date().isoformat()
                )
                status = normalize_status(value(row, s, "status"))
                oid = value(row, s, "order_id")
                if s.role != "ledger" and (not isinstance(oid, str) or not oid.strip()):
                    raise ParseError("missing_order_id", "order key missing")
            except ParseError as exc:
                issue(exc.code, ref, ["revenue", "order_count"])
                continue
            quantity = value(row, s, "quantity")
            if quantity is not None and str(quantity).lstrip().startswith("-"):
                if not negative_returns:
                    issue("return_quantity_policy_unknown", ref, ["revenue", "order_count"])
                else:
                    context.rules.require("return_policy")
                    return_events.setdefault(oid, []).append(ref)
                    if status == "cancelled":
                        issue("return_cancel_conflict", ref, ["revenue", "order_count"])
                # A return event cannot move the original order's creation date/clock.
                continue
            key = fingerprint(
                {
                    "split": context.catalog["split"],
                    "business": context.catalog["business_id"],
                    "kind": "order",
                    "id": oid,
                }
            )
            old = headers.get(oid)
            record = (
                key,
                day,
                status,
                str(value(row, s, "customer_id", "")),
                str(value(row, s, "customer", "")),
            )
            if old and old[:3] != record[:3]:
                issue("order_conflict", ref, ["revenue", "order_count"])
            if old and old[3] != record[3]:
                group_errors.add("customer_group_share")
            headers[oid] = record
            conn.execute(
                "INSERT OR IGNORE INTO orders VALUES (?,?,?,?,?)",
                (key, oid, day, status, "resolved"),
            )
            raw_clock = value(
                row, s, "clock", date_value if re.search(r"\d:\d", str(date_value)) else None
            )
            if raw_clock is not None:
                try:
                    clock_text = str(raw_clock).strip()
                    if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?", clock_text):
                        parts = clock_text.split(":")
                        hour, minute = int(parts[0]), int(parts[1])
                        second = Decimal(parts[2]) if len(parts) == 3 else Decimal(0)
                        if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
                            raise ParseError("clock_format", "invalid time of day")
                        clock_day = dt.date()
                        seconds = Decimal(hour * 3600 + minute * 60) + second
                    elif re.fullmatch(r"\d+(?:\.\d+)?", str(raw_clock)):
                        if not table.metadata.get("date_system"):
                            raise ParseError("clock_epoch", "timestamp epoch missing")
                        number = Decimal(str(raw_clock))
                        whole = int(number)
                        clock_day = (
                            dt.date()
                            if whole == 0
                            else parse_date(str(whole), date_system=table.metadata["date_system"])
                        )
                        with localcontext() as arithmetic:
                            arithmetic.prec = len(str(raw_clock)) + 20
                            seconds = (number - whole) * 86400
                        clock_cell = row.cells.get(s.fields.get("clock", s.fields["ordered_at"]))
                        style = int(clock_cell.metadata.get("style") or 0)
                        styles = table.metadata.get("styles", [])
                        code = styles[style].get("format_code") if style < len(styles) else None
                        # Explicit whole-second display precision removes sub-microsecond
                        # binary Excel serial noise; keep arbitrary fractional formats exact.
                        if code and "ss" in code.lower() and not re.search(r"s[.,]0", code.lower()):
                            seconds = seconds.quantize(Decimal(1), rounding=ROUND_HALF_UP)
                    else:
                        clock_dt = parse_datetime(raw_clock)
                        clock_day = clock_dt.date()
                        seconds = (
                            Decimal(clock_dt.hour * 3600 + clock_dt.minute * 60 + clock_dt.second)
                            + Decimal(clock_dt.microsecond) / 1000000
                        )
                    if clock_day.isoformat() != day[:10]:
                        raise ParseError("clock_date_conflict", "date/clock disagree")
                    old_time = conn.execute(
                        "SELECT seconds_of_day FROM order_times WHERE order_key=?", (key,)
                    ).fetchone()
                    if old_time and Decimal(old_time[0]) != seconds:
                        raise ParseError("clock_conflict", "conflicting order clock")
                    conn.execute(
                        "INSERT OR IGNORE INTO order_times VALUES (?,?,?)",
                        (key, str(seconds), dumps(ref)),
                    )
                except ParseError as exc:
                    issue(exc.code, ref, ["shift_revenue", "shift_order_count"])
            if "channel" in s.fields:
                channel = str(value(row, s, "channel", ""))
                if not channel.strip():
                    issue("channel_missing", ref, ["channel_share"])
                old_channel = conn.execute(
                    "SELECT channel FROM order_scopes WHERE order_key=?", (key,)
                ).fetchone()
                if old_channel and old_channel[0] != channel:
                    issue("channel_conflict", ref, ["channel_share"])
                conn.execute("INSERT OR IGNORE INTO order_scopes VALUES (?,?)", (key, channel))
            customer_id = record[3]
            group = None
            if customer_id:
                if customer_id not in customers:
                    if any(x.role == "customer" for x in mapping.sources):
                        group_errors.add("customer_group_share")
                else:
                    group = customers[customer_id][1]
            elif not customer_id and any(x.role == "customer" for x in mapping.sources):
                group = ""
            if group is not None:
                conn.execute("INSERT OR IGNORE INTO order_groups VALUES (?,?)", (key, str(group)))
    for oid, refs in return_events.items():
        if oid not in headers:
            issue("orphan_return", refs[0], ["revenue", "order_count"])
            continue
        key, _, status, *_ = headers[oid]
        if status == "cancelled":
            issue("return_cancel_conflict", refs[0], ["revenue", "order_count"])
        conn.execute("UPDATE orders SET status='returned' WHERE order_key=?", (key,))
    for s in mapping.sources:
        if s.role not in {"return", "return_quantity"}:
            continue
        for table, row in data_rows(context, s):
            oid = value(row, s, "order_id")
            quantity = value(row, s, "returned_quantity", 0)
            if s.role == "return_quantity":
                try:
                    if Decimal(str(quantity)) <= 0:
                        continue
                except ArithmeticError:
                    issue("return_quantity_error", row.reference(table), ["revenue", "order_count"])
                    continue
            if oid not in headers:
                issue("orphan_return", row.reference(table), ["revenue", "order_count"])
                continue
            key, _, status, *_ = headers[oid]
            if status == "cancelled":
                issue("return_cancel_conflict", row.reference(table), ["revenue", "order_count"])
            conn.execute("UPDATE orders SET status='returned' WHERE order_key=?", (key,))

    def line_inputs():
        for source in mapping.sources:
            if source.role not in {"combined", "detail", "ledger"}:
                continue
            for table, row in data_rows(context, source):
                if source.role == "ledger":
                    try:
                        raw_date = value(row, source, "ordered_at")
                        cell = row.cells.get(source.fields["ordered_at"])
                        dt = parse_datetime(
                            raw_date,
                            year=_sheet_year(source.profile),
                            date_system=date_systems[source.profile["source_id"]]
                            if cell and re.fullmatch(r"\d+(?:\.\d+)?", str(cell.raw_value))
                            else None,
                        )
                        day = (
                            dt.isoformat()
                            if re.search(r"\d:\d", str(raw_date))
                            else dt.date().isoformat()
                        )
                        status = normalize_status(value(row, source, "status"))
                    except ParseError as exc:
                        issue(exc.code, row.reference(table), ["revenue", "order_count"])
                        continue
                    yield source, table, row, None, day, status
                else:
                    oid = value(row, source, "order_id")
                    if oid not in headers:
                        issue("orphan_detail", row.reference(table), ["revenue", "order_count"])
                        continue
                    key, day, status, *_ = headers[oid]
                    yield source, table, row, key, day, status

    for s, table, row, key, day, status in line_inputs():
        ref = row.reference(table)
        raw_id = row.identity(table)
        item = value(row, s, "item")
        product_id = str(value(row, s, "product_id", ""))
        codes = product_name_codes.get(identity_text(str(item)), set())
        master = (
            products.get(product_id)
            if product_id
            else (products[next(iter(codes))] if len(codes) == 1 else None)
        )
        if not product_id and master is None and item is not None:
            # User policy 2026-11-04: only an unaccented spelling with one catalog
            # code may use this fallback. A coded line never falls back by name.
            # Conflicting groups or multiple codes remain unresolved.
            alias_codes = product_alias_codes.get(token(str(item)), set())
            if identity_text(str(item)).isascii() and len(alias_codes) == 1:
                master = products[next(iter(alias_codes))]
                if master[1] is not None:
                    name_policy_matches["unaccented_unique_catalog_code"] += 1
        if item is None and master:
            item = master[0]
        group = master[1] if master else value(row, s, "item_group")
        role = "sale_item"
        quantity = value(row, s, "quantity")
        if quantity is not None and str(quantity).lstrip().startswith("-"):
            if not negative_returns:
                issue("return_quantity_policy_unknown", ref, ["revenue", "order_count"])
            else:
                context.rules.require("return_policy")
                role = "return_item"
                if key:
                    conn.execute("UPDATE orders SET status='returned' WHERE order_key=?", (key,))
                else:
                    status = "returned"
        if not item:
            role = "unknown"
            issue("item_role_unknown", ref, ["revenue"])
        name = normalized(str(item)).strip()
        if name == "phi phuc vu":
            try:
                context.rules.require("item_role")
                role = "service_fee"
            except ContractError:
                role = "unknown"
                issue("fee_policy_unknown", ref, ["revenue"])
            group = ""
        elif re.search(r"voucher|diem thuong|phi giao|phi ship", name):
            if any(
                word in normalized(context.catalog["readme"])
                for word in ["voucher", "diem", "phi giao"]
            ):
                role = "order_adjustment"
            else:
                role = "unknown"
                issue("adjustment_policy_unknown", ref, ["revenue"])
        amount = None
        try:
            cell = row.cells.get(s.fields["amount"])
            amount = parse_money(
                cell_value(cell),
                "decimal_dot"
                if cell and cell.native_type in {"numeric", "integer"}
                else conventions[s.profile["source_id"]],
            )
            if amount < 0 and role not in {"order_adjustment", "return_item"}:
                raise ParseError("negative_role_unknown", "negative line role unresolved")
        except ParseError as exc:
            issue(exc.code, ref, ["revenue"])
        signature = _signature(row.cells)
        line_key = (
            fingerprint(
                {
                    "order_key": key,
                    "source_values": signature,
                    "year": _sheet_year(s.profile) if mapping.ledger else None,
                }
            )
            if key
            else raw_id
        )
        if mapping.ledger:
            conn.execute(
                "INSERT INTO sales_events VALUES (?,?,?,?,?,?)",
                (
                    raw_id,
                    day,
                    status,
                    str(amount) if amount is not None else None,
                    role,
                    dumps(ref),
                ),
            )
            if group is not None:
                conn.execute("INSERT INTO event_groups VALUES (?,?)", (raw_id, str(group)))
        else:
            duplicate = conn.execute(
                "SELECT 1 FROM order_lines WHERE line_key=?", (line_key,)
            ).fetchone()
            conn.execute(
                "INSERT OR IGNORE INTO order_lines VALUES (?,?,?,?,?,?)",
                (line_key, key, str(amount) if amount is not None else None, role, signature, None),
            )
            if (
                any(x.role == "product" and "item_group" in x.fields for x in mapping.sources)
                or "item_group" in s.fields
            ):
                if group is None and role != "order_adjustment":
                    group_errors.add("item_group_share")
                else:
                    conn.execute(
                        "INSERT OR IGNORE INTO line_groups VALUES (?,?)",
                        (line_key, str(group or "")),
                    )
            conn.execute(
                "INSERT INTO lineage VALUES (?,?,?,?,?,?,?)",
                (
                    raw_id,
                    s.profile["source_id"],
                    row.ordinal,
                    "duplicate" if duplicate else "kept",
                    line_key,
                    dumps(ref),
                    dumps(["read_raw", "checked_header_detail_join"]),
                ),
            )
    for metric in group_errors:
        issue("missing_group_reference", primary[0].profile["source_ref"], [metric])
    audit_sources = []
    for source in mapping.sources:
        profile = source.profile
        if source.role == "auxiliary":
            audit_sources.append(
                {
                    "source_id": profile["source_id"],
                    "raw_rows": profile["raw_row_count"],
                    "disposition": "unused_for_sales_metrics",
                }
            )
            continue
        table = context.source_table(profile["source_id"])
        for row in table.iter_rows():
            raw_id = row.identity(table)
            blank = not any(c.raw_value not in {None, ""} for c in row.cells.values())
            disposition = (
                "blank"
                if blank
                else "header"
                if row.row in profile.get("header_rows", [])
                else "reference_" + source.role
            )
            conn.execute(
                "INSERT OR IGNORE INTO lineage VALUES (?,?,?,?,?,?,?)",
                (
                    raw_id,
                    profile["source_id"],
                    row.ordinal,
                    disposition,
                    None,
                    dumps(row.reference(table)),
                    dumps(["read_raw", "source_role:" + source.role]),
                ),
            )
        audit_sources.append(
            {
                "source_id": profile["source_id"],
                "raw_rows": profile["raw_row_count"],
                "lineage_rows": conn.execute(
                    "SELECT COUNT(*) FROM lineage WHERE source_id=?", (profile["source_id"],)
                ).fetchone()[0],
            }
        )
        if audit_sources[-1]["lineage_rows"] != profile["raw_row_count"]:
            raise ContractError("required-source row accounting mismatch")
    if not mapping.ledger:
        missing = conn.execute(
            "SELECT order_key FROM orders WHERE status IN ('completed','shipping') "
            "AND order_key NOT IN (SELECT order_key FROM order_lines) LIMIT 1"
        ).fetchone()
        if missing:
            issue("eligible_order_without_lines", primary[0].profile["source_ref"], ["revenue"])
    if not mapping.ledger:
        joins.append(
            {
                "kind": "header_detail",
                "cardinality": "header_1_detail_n",
                "header_keys": len(headers),
                "orphan_details": counts["orphan_detail"],
                "header_conflicts": counts["order_conflict"],
                "payments_joined": False,
            }
        )
    dates = conn.execute(
        "SELECT MIN(occurred_at),MAX(occurred_at) FROM sales_events"
        if mapping.ledger
        else "SELECT MIN(ordered_at),MAX(ordered_at) FROM orders"
    ).fetchone()
    observed = {"start": dates[0][:10], "end": dates[1][:10]} if dates[0] else None
    full = re.search(
        r"du lieu day du tu (\d{1,2}/\d{1,2}/\d{4}) den het (\d{1,2}/\d{1,2}/\d{4})",
        normalized(context.catalog["readme"]),
    )
    span = (
        {"start": parse_date(full[1]).isoformat(), "end": parse_date(full[2]).isoformat()}
        if full
        else observed
    )
    schema_fields = set().union(*(s.fields for s in mapping.sources))
    if conn.execute("SELECT 1 FROM order_times LIMIT 1").fetchone():
        schema_fields.add("clock")
    if any(s.role == "product" and "item_group" in s.fields for s in mapping.sources):
        schema_fields.add("item_group")
    if any(s.role == "customer" and "customer_group" in s.fields for s in mapping.sources):
        schema_fields.update({"customer_group", "customer"})
    return {
        "status": "blocked" if blocked else "ready",
        "shape": "sales_ledger" if mapping.ledger else "relational_orders",
        "source_id": primary[0].profile["source_id"],
        "source_ids": [s.profile["source_id"] for s in mapping.sources],
        "isolated_source_families": [
            {
                "source_id": s.profile["source_id"],
                "family": s.auxiliary_family,
                "status": "unsupported_operator",
                "authority": context.catalog["readme_hash"],
                "sales_isolation": "explicit_nonfinancial_grain_and_all_column_meanings",
            }
            for s in mapping.sources
            if s.auxiliary_family
        ],
        "mapping": {
            "fields": {f: True for f in schema_fields},
            "date_systems": date_systems,
            "date_system_basis": (
                "Workbook epoch or unique CSV epoch within README end; no calendar inference"
            ),
            "money_conventions": conventions,
            "money_policy_sources": money_policy_sources,
            "money_basis": "user_2026_11_04_consistent_three_digit_groups"
            if money_policy_sources
            else "README_or_unambiguous_source_tokens",
            "money_warning": "No locale; money groups follow the user-approved export policy."
            if money_policy_sources
            else None,
            "name_matching_basis": "user_2026_11_04_unaccented_unique_catalog_code",
            "name_policy_matches": dict(name_policy_matches),
            "name_matching_warning": (
                "Unaccented names may join one catalog code under user policy; "
                "case/spacing/punctuation normalized, multi-code or group conflicts block."
            ),
            "authority": {k: context.catalog[k] for k in ["readme_hash", "rulebook_hash"]},
        },
        "counts": {
            t: conn.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
            for t in ["orders", "order_lines", "sales_events", "lineage", "issues"]
        },
        "issue_counts": dict(counts),
        "blocking_metrics": sorted(blocked),
        "joins": joins,
        "source_row_accounting": audit_sources,
        "coverage": {
            "observed_span": observed,
            "available_span": span,
            "parse_status": "partial" if "order_count" in blocked else "complete",
            "availability_basis": ["README complete span"]
            if full
            else ["observed_sales_record_dates" if mapping.ledger else "observed_order_dates"]
            if observed
            else [],
        },
        "missing_information": ["order_identity"] if mapping.ledger else [],
        "metric_execution": "separate_query_time_executor",
    }
