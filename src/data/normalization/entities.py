"""Evidence-backed identities and business facts, extending the existing prepared store.

No question or labels enter preparation. Optional families fail independently: a
missing payment key cannot invalidate an otherwise checked sales revenue export.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import replace
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from typing import TYPE_CHECKING

from src.data.cache import fingerprint
from src.data.normalization.relational import (
    LABELS,
    MEANINGS,
    MappedSource,
    abbreviation_table,
    data_rows,
    token,
)
from src.data.normalization.sales import SalesMapping, is_walk_in
from src.data.normalization.stock import identity_text
from src.data.normalization.values import (
    PURCHASE_CANCELLED,
    PURCHASE_RECEIVED,
    cell_value,
    consistent_grouping,
    number_evidence,
    parse_date,
    parse_datetime,
    parse_money,
    parse_number,
    strip_unit,
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
CREATE INDEX entities_kind ON entities(kind);
CREATE INDEX payments_day ON payments(substr(paid_at,1,10));
CREATE INDEX purchases_day ON purchases(substr(purchased_at,1,10));
CREATE INDEX lot_allocations_lot ON lot_allocations(lot_id);
CREATE INDEX line_items_product ON line_items(product_key);
CREATE INDEX payments_order_date ON payments(order_key,paid_at);
CREATE INDEX stock_product_date ON stock_balances(product_key,occurred_at);
CREATE INDEX lots_product ON lots(product_key);
CREATE INDEX lot_allocations_line ON lot_allocations(line_key);
"""

EXTRA_MEANINGS = {
    "snapshot_balance": {
        "ton kho la ton cuoi ngay cuoi co du lieu, theo don vi co ban",
        "ton cuoi ngay cuoi co du lieu, theo don vi co ban",
    },
    "unit_cost": {"gia von mot don vi ban"},
    "base_quantity": {"so luong da doi ra don vi co ban", "base quantity"},
    "base_unit": {"don vi co ban", "base unit"},
    "unit": {"don vi tinh", "selling unit"},
    "payable": {"tong tien don", "order payable", "so khach phai tra cho ca don"},
    "payment_method": {
        "hinh thuc thanh toan cua don: cash tien mat, transfer chuyen khoan, "
        "ewallet vi dien tu, cod giao hang thu tien, debt ghi no",
        "order payment method",
    },
    "payment_id": {"ma phieu thu", "payment id"},
    "paid_at": {"thoi diem thu", "payment date"},
    "paid_amount": {"so tien", "payment amount", "so tien tra lan do"},
    # Purchase receipt headers, as shop READMEs describe them.
    "purchase_id": {"ma phieu nhap"},
    "purchased_at": {"ngay nhap"},
    "purchase_amount": {"tong tien phieu nhap", "tong tien cua ca phieu nhap"},
    "status": {"trang thai phieu nhap"},
}
EXTRA_LABELS = {
    "product_id": {"idsp"},
    "item": {"hanghoa"},
    "base_quantity": {"slquydoi", "soluongquydoi", "slcoban"},
    "base_unit": {"donvicoban", "donvicoso", "dvtcoban", "donvi"},
    "unit": {"donvitinh", "dvt", "dvi"},
    "payable": {"tongthanhtoan", "tongtien"},
    "payment_method": {"phuongthuctt", "hinhthucthanhtoan", "phuongthucthanhtoan"},
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
    # POS READMEs state conventions as sentences: "**Giá trị** là số tiền trả lần đó".
    # They only add meanings; they never hide a column's label.
    sentences = {
        plain(k): plain(v) for k, v in re.findall(r"\*\*([^*\n]+?)\*\* là ([^;.\n]+)", readme)
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
            or sentences.get(plain(c["display_name"])) in meanings
            or (
                pos
                and plain(c["display_name"]) not in definitions
                and token(c["display_name"]) in labels
            )
        ]
        if len(candidates) == 1:
            result[key] = candidates[0]
    if pos:
        # Keys and dates of a payment, receipt or stock report are named only inside the
        # table whose amount column the README itself defines.
        tokens = {token(c["display_name"]): c["position"] for c in profile["columns"]}
        contextual = {
            "paid_amount": {
                "payment_id": {"maphieu", "maphieuthu", "sophieu"},
                "paid_at": {"thoigian", "ngay", "ngaythu", "thoigianthu"},
                "order_id": {"mahoadon", "madon", "madonhang"},
            },
            "purchase_amount": {
                "purchase_id": {"manhaphang", "maphieunhap", "sophieunhap"},
                "purchased_at": {"thoigian", "ngay", "ngaynhap"},
                "status": {"trangthai"},
            },
            "snapshot_balance": {
                "product_id": {"mahang", "masp", "masanpham"},
                "base_unit": {"dvt", "donvitinh", "donvi"},
            },
        }
        for anchor, fields in contextual.items():
            if anchor not in result:
                continue
            for key, labels in fields.items():
                found = [p for t, p in tokens.items() if t in labels]
                # A stock report's unit column is the base unit (README "theo đơn vị cơ bản").
                # A payment's or receipt's own time column also reads as an order time.
                taken = {p for k, p in result.items() if k not in {"unit", "ordered_at"}}
                if key not in result and len(found) == 1 and found[0] not in taken:
                    result[key] = found[0]
    return result


def _ascii_fold(text: str) -> str:
    """Letters and digits without diacritics, lower case ("Bánh Đa" -> "banhda")."""
    folded = unicodedata.normalize("NFD", text.replace("đ", "d").replace("Đ", "D"))
    return "".join(c for c in folded.casefold() if c.isalnum() and not unicodedata.combining(c))


def _letters(text: str) -> str:
    """Letters and digits as written, lower case; a spelling without diacritics equals its
    own ``_ascii_fold``."""
    return "".join(c for c in unicodedata.normalize("NFC", text).casefold() if c.isalnum())


def conversion_table(context: BusinessContext):
    """Unit conversions the shop lists: a conversion table (README column meanings or
    headers "Đơn vị quy đổi / Tỉ lệ quy đổi / Đơn vị cơ bản") or a README list
    ("- Coca Cola 330ml: 1 thùng = 24 lon").

    Returns (product name -> (base unit, {large unit: base units per large unit}),
    whether the list holds only items that have a large unit) or None. Keys are
    identity_text; two base units for one item, two ratios for one unit, a large unit equal
    to the base or an unreadable ratio make the whole list unusable (None).
    """
    raw_readme = context.catalog["readme"]
    readme = plain(raw_readme)
    if not re.search(r"quy doi don vi(?:\*\*)?: so luong ghi theo don vi cua dong", readme):
        return None
    table: dict[str, tuple[str, dict[str, Decimal]]] = {}

    def add(name, base, large, ratio) -> bool:
        if ratio <= 0:
            return False
        name, base, large = (identity_text(str(v)) for v in (name, base, large))
        old_base, larges = table.setdefault(name, (base, {}))
        if old_base != base or larges.get(large, ratio) != ratio or large == base:
            return False
        larges[large] = ratio
        return True

    listed = re.findall(
        r"^\s*-\s+(.+?):\s*1\s+(.+?)\s*=\s*(\d+(?:[.,]\d+)?)\s+(.+?)\s*$", raw_readme, re.M
    )
    for name, large, ratio, base in listed:
        if not add(name, base, large, parse_number(ratio.replace(",", "."))):
            return None
    # "- **Hàng theo bó**: A, B, C bán theo bó, 1 bó = 10 cây": a large unit for listed items.
    for items, large, ratio, base in re.findall(
        r"^\s*-\s+\*\*[^*]+\*\*\s*:\s*(.+?)\s+bán theo (\w+),\s*1 \2\s*=\s*(\d+)\s+(\w+)",
        raw_readme,
        re.M,
    ):
        for name in (n.strip() for n in items.split(",")):
            if not name or not add(name, base, large, parse_number(ratio)):
                return None
    only_large = bool(listed)
    sections = re.findall(
        r"^## Cột trong `([^`]+)`(?:, sheet `([^`]+)`)?\s*\n(.*?)(?=^## |\Z)",
        raw_readme,
        re.M | re.S,
    )
    headers = {
        "name": r"ten (?:san pham|hang|mat hang)",
        "large": r"don vi quy doi",
        "ratio": r"(?:ti|ty) le quy doi",
        "base": r"don vi co ban",
    }
    meanings = {
        "name": r"ten (?:hang|mon|san pham)\b.*",
        "large": r"don vi lon\b.*",
        "ratio": r"he so: so don vi co ban trong 1 don vi lon",
        "base": r"don vi co ban",
    }
    found = []
    for profile in context.catalog["tables"]:
        if profile.get("structure_status") != "resolved":
            continue
        file = Path(profile["source_ref"]["file"]).name
        bodies = [b for f, sh, b in sections if f == file]
        definitions = {
            plain(k): plain(v)
            for body in bodies
            for k, v in re.findall(r"^\s*-\s+\*\*(.+?)\*\*\s*:\s*(.+)$", body, re.M)
        }
        roles: dict[str, list[int]] = {}
        for column in profile.get("columns", []):
            name = plain(column["display_name"])
            for role in headers:
                meaning = definitions.get(name)
                if re.fullmatch(headers[role], name) or (
                    meaning and re.fullmatch(meanings[role], meaning)
                ):
                    roles.setdefault(role, []).append(column["position"])
        if set(roles) == set(headers) and all(len(p) == 1 for p in roles.values()):
            name_meaning = next(
                (
                    definitions.get(plain(c["display_name"]), "")
                    for c in profile["columns"]
                    if c["position"] == roles["name"][0]
                ),
                "",
            )
            found.append((profile, {k: v[0] for k, v in roles.items()}, name_meaning))
    if len(found) > 1:
        return None
    if found:
        profile, roles, name_meaning = found[0]
        header = set(profile.get("header_rows", []))
        chained: list[tuple] = []
        for row in context.source_table(profile["source_id"]).iter_rows():
            if row.row in header or (header and row.row is not None and row.row < min(header)):
                continue
            cells = {k: row.cells.get(p) for k, p in roles.items()}
            raw = {k: (None if c is None else c.raw_value) for k, c in cells.items()}
            if all(v in {None, ""} for v in raw.values()):
                continue
            if any(v in {None, ""} for v in raw.values()):
                return None
            # "15 vỉ": the ratio counts the unit named (README: "số kèm tên đơn vị là tính
            # theo đơn vị đó, chỉ có số là tính theo đơn vị cơ bản").
            named = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s+(\D+?)\s*", str(raw["ratio"]))
            if named:
                chained.append((raw["name"], raw["base"], raw["large"], named[1], named[2]))
                continue
            try:
                ratio = parse_number(raw["ratio"])
            except ContractError:
                return None
            if not add(raw["name"], raw["base"], raw["large"], ratio):
                return None
        pending = chained
        while pending:
            left = []
            for name, base, large, count, unit in pending:
                known = table.get(identity_text(str(name)), (None, {}))
                unit_key = identity_text(unit)
                if unit_key == identity_text(str(base)):
                    per = Decimal(1)
                elif unit_key in known[1]:
                    per = known[1][unit_key]
                else:
                    left.append((name, base, large, count, unit))
                    continue
                if not add(name, base, large, parse_number(count.replace(",", ".")) * per):
                    return None
            if len(left) == len(pending):
                return None  # a chain that never reaches the base unit
            pending = left
        only_large = (
            only_large
            or "chi co mat hang co don vi lon" in name_meaning
            or ("chi co mat hang co don vi lon" in readme)
        )
    return (table, only_large) if table else None


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
    # Rulebook: names differing only in writing are the same thing. Accent-free lookup
    # is a fallback used only when it names exactly one catalog entity.
    bare = {"product": {}, "customer": {}}
    abbreviations = abbreviation_table(context)
    conversions = conversion_table(context)
    # Items absent from an only-large-unit list: the unit each is sold in (one per item).
    sold_units: dict[str, dict[str, list[str]]] = {}

    def entity(kind, code, name, unit, ref):
        code, name = str(code or "").strip(), str(name or "").strip()
        if not code and not name:
            raise ContractError("missing_entity_identity")
        # Walk-in sales (README: "Khách lẻ nếu không lưu") are often abbreviated in the
        # source; rulebook §3 never counts or ranks them as customers.
        if not code and kind == "customer" and is_walk_in(name):
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

    customer_master = any(s.role == "customer" for s in sources)
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
                masters[kind][str(code).strip()] = key
                names[kind].setdefault(identity_text(str(name)), set()).add(key)
                bare[kind].setdefault(token(str(name)), set()).add(key)
            except ContractError as exc:
                fail(kind, str(exc))
    orders = dict(conn.execute("SELECT raw_order_id, order_key FROM orders"))
    lineage = dict(conn.execute("SELECT raw_id, line_key FROM lineage WHERE line_key IS NOT NULL"))
    line_numbers = {}
    units = {}
    spellings: dict[str, set[str]] = {}
    for s in sources:
        if s.role in {"combined", "detail"} and "item" in s.fields:
            for _, row in data_rows(context, s):
                n = get(row, s.fields, "item")
                u = get(row, s.fields, "unit") if "unit" in s.fields else None
                if n:
                    n = abbreviations.get(identity_text(str(n)), n)
                    spellings.setdefault(_ascii_fold(str(n)), set()).add(str(n))
                if n and u:
                    units.setdefault(identity_text(str(n)), set()).add(str(u))
    # Rulebook §8: one item written two ways ("Bánh tiramisu", "Banh tiramisu") is one item.
    # A spelling without any diacritic joins the single spelling with them; two accented
    # spellings of the same letters are different names and stay apart.
    twins: dict[str, str] = {}
    for group in spellings.values():
        keys = {identity_text(n): n for n in group}
        accented = {k: n for k, n in keys.items() if _letters(n) != _ascii_fold(n)}
        plain_keys = [k for k in keys if k not in accented]
        if len(accented) == 1 and plain_keys:
            ((target_key, target),) = accented.items()
            for k in plain_keys:
                twins[k] = target
                units.setdefault(target_key, set()).update(units.pop(k, set()))
    # A listed item's base unit is the conversion list's, not the unit it happens to sell in.
    if conversions:
        for listed_name, (listed_base, _) in conversions[0].items():
            units[listed_name] = {listed_base}
    # In code-less flat exports, only a single observed selling unit is a proved
    # base unit. A converted quantity may be ranked without claiming its unit.
    for s in sources:
        if s.role not in {"combined", "header", "detail"}:
            continue
        for table, row in data_rows(context, s):
            f, ref = s.fields, row.reference(table)
            oid = str(get(row, f, "order_id") or "").strip()
            order = orders.get(oid)
            if not order:
                continue
            if s.role in {"combined", "header"}:
                try:
                    code, name = get(row, f, "customer_id"), get(row, f, "customer")
                    key = masters["customer"].get(str(code).strip()) if code else None
                    if code and not key and not customer_master:
                        # No customer list: rulebook §3, each customer code is one customer.
                        key = entity("customer", code, name, None, ref)
                        masters["customer"][str(code).strip()] = key
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
                if not code and name is not None:
                    # The shop's own abbreviation list names the full item ("Cáp C").
                    name = abbreviations.get(identity_text(str(name)), name)
                    name = twins.get(identity_text(str(name)), name)
                key = masters["product"].get(str(code).strip()) if code else None
                if code and not key:
                    raise ContractError("orphan_product_code")
                if not code and masters["product"]:
                    candidates = names["product"].get(identity_text(str(name)), set())
                    if not candidates:
                        candidates = bare["product"].get(token(str(name)), set())
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
                        if field == "sold_quantity":
                            raw = strip_unit(raw, get(row, f, "unit"))
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
                # A sold quantity stands in for the base quantity only when the line is sold
                # in the product's base unit; a case of 24 cans is not one can.
                unit = get(row, f, "unit") if "unit" in f else None
                if numbers[0] is None and unit and numbers[1] is not None and conversions:
                    # README "Quy đổi đơn vị": the line's quantity is in the line's unit.
                    listed_table, only_large = conversions
                    item_name = conn.execute(
                        "SELECT name FROM entities WHERE entity_key=?", (key,)
                    ).fetchone()[0]
                    listed = listed_table.get(identity_text(str(item_name)))
                    sold_unit = identity_text(str(unit))
                    if listed:
                        base, larges = listed
                        if sold_unit == base:
                            numbers[0] = numbers[1]
                        elif sold_unit in larges:
                            numbers[0] = str(Decimal(numbers[1]) * larges[sold_unit])
                        conn.execute(
                            "UPDATE entities SET base_unit=? "
                            "WHERE entity_key=? AND base_unit IS NULL",
                            (base, key),
                        )
                    elif only_large:
                        sold_units.setdefault(key, {}).setdefault(sold_unit, []).append(line)
                        numbers[0] = numbers[1]
                if numbers[0] is None and unit:
                    base_unit = conn.execute(
                        "SELECT base_unit FROM entities WHERE entity_key=?", (key,)
                    ).fetchone()[0]
                    if base_unit and identity_text(str(unit)) != identity_text(base_unit):
                        numbers[1] = None
                    elif base_unit:
                        # Sold in the product's own base unit: the quantity is already base.
                        numbers[0] = numbers[1]
                conn.execute(
                    "INSERT OR IGNORE INTO line_items VALUES (?,?,?,?,?,?)",
                    (line, key, *numbers, dumps(ref)),
                )
            except ContractError as exc:
                fail("product", str(exc))
    # An unlisted item sold in one unit has that unit as its base unit; in two, neither.
    for key, units in sold_units.items():
        if len(units) == 1:
            conn.execute(
                "UPDATE entities SET base_unit=? WHERE entity_key=? AND base_unit IS NULL",
                (next(iter(units)), key),
            )
        else:
            for lines in units.values():
                conn.executemany(
                    "UPDATE line_items SET base_quantity=NULL WHERE line_key=?",
                    [(line,) for line in lines],
                )
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
                        oid = orders.get(str(get(row, f, "order_id")).strip())
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
                        if plain(status) not in PURCHASE_RECEIVED | PURCHASE_CANCELLED:
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
                        product = masters["product"].get(str(get(row, f, "product_id")).strip())
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
    # A snapshot names its items by code, or by name when the shop keeps no codes.
    snapshots = [
        s
        for s in sources
        if {"base_unit", "snapshot_balance"} <= s.fields.keys()
        and ({"product_id", "item"} & s.fields.keys())
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
            if not re.search(r"\bla ton cuoi ngay cuoi co du lieu, theo don vi co ban\b", text):
                raise ContractError("snapshot_base_unit_authority_missing")
            dates = re.findall(r"du lieu co den het (\d{1,2}/\d{1,2}/\d{4})", text)
            if len(set(dates)) != 1:
                raise ContractError("snapshot_end_date_missing_or_ambiguous")
            snapshot_at = parse_date(dates[0]).isoformat()
            source = snapshots[0]
            seen = set()
            by_name: dict[str, set[str]] = {}
            if "product_id" not in source.fields:
                for key, name in conn.execute(
                    "SELECT entity_key,name FROM entities WHERE kind='product'"
                ):
                    by_name.setdefault(identity_text(str(name)), set()).add(key)
            for table, row in data_rows(context, source):
                if "product_id" in source.fields:
                    code = str(get(row, source.fields, "product_id")).strip()
                    product = masters["product"].get(code)
                else:
                    # Exactly one catalog item of that name, else the snapshot is unusable.
                    found = by_name.get(identity_text(str(get(row, source.fields, "item"))), ())
                    product = next(iter(found)) if len(found) == 1 else None
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
                        key = orders.get(str(get(row, s.fields, "order_id")).strip())
                        at = timestamp(row, s, "returned_at")
                        if not key or (key in returns and returns[key] != at):
                            raise ContractError("lot_return_date_missing_or_conflicting")
                        returns[key] = at
            for s in lot_sources:
                for table, row in data_rows(context, s):
                    f = s.fields
                    product = masters["product"].get(str(get(row, f, "product_id")).strip())
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
                    order = orders.get(str(get(row, f, "order_id")).strip())
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
