"""Purchase-receipt questions read from the shop's receipt tables (rulebook §5).

- Giá nhập bình quân một mặt hàng trong tháng: weighted by the quantity bought, in the unit
  the receipt line was bought in, over receipts dated in the month that are not cancelled.
  Only when the split's rulebook states this rule (`weighted_purchase_price`).
- Nhà cung cấp nhập nhiều nhất: the supplier with the largest purchase amount in the period
  ("Tiền nhập hàng trong kỳ"), named as the shop records it.

Receipt lines are found by their columns (receipt id, product, quantity, unit, price); their
date and status come from the same table or from the receipt table joined by receipt id.
An unknown status, a cancelled-receipt note without a status column, a product bought in
another unit in the same month, or a tie abstains.
"""

from __future__ import annotations

import re
from contextlib import closing
from decimal import Decimal

from src.data.catalog import BusinessContext
from src.data.normalization.values import PURCHASE_CANCELLED, PURCHASE_RECEIVED, parse_date
from src.jsonio import ContractError
from src.semantics.rulebook import plain

ROLES = {
    "receipt": r"ma nhap hang|ma phieu nhap|so phieu|ma don nhap",
    "date": r"thoi gian|ngay nhap(?: kho)?",
    "status": r"trang thai|tinh trang",
    "note": r"ghi chu|noi dung|dien giai|mo ta",
    "supplier": r"nha cung cap|ma ncc|ma nha cung cap",
    "code": r"ma hang(?: hoa)?|ma sku|ma sp",
    "name": r"ten mat hang|ten hang|product",
    "unit": r"dvt(?: nhap)?|don vi tinh",
    "quantity": r"so luong(?: nhap)?|sl(?: nhap)?",
    "price": r"don gia(?: nhap)?|gia nhap",
    "total": r"thanh tien|can tra ncc|tong gia tri nhap",
}


def _header(name: str) -> str:
    return plain(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(name)))


def _tables(context: BusinessContext) -> list[dict]:
    """Resolved tables with their recognised role columns and rows."""
    tables = []
    for profile in context.catalog["tables"]:
        if profile.get("structure_status") != "resolved":
            continue
        roles: dict[str, list[int]] = {}
        for column in profile.get("columns", []):
            name = _header(column["display_name"])
            for role, pattern in ROLES.items():
                if re.fullmatch(pattern, name):
                    roles.setdefault(role, []).append(column["position"])
        if "receipt" not in roles:
            continue
        if any(len(positions) != 1 for positions in roles.values()):
            continue
        header = set(profile.get("header_rows", []))
        rows = []
        source = context.source_table(profile["source_id"])
        epoch = source.metadata.get("date_system")
        for row in source.iter_rows():
            if row.row in header or (header and row.row is not None and row.row < min(header)):
                continue
            values = {}
            for role, (position,) in roles.items():
                cell = row.cells.get(position)
                raw = None if cell is None else cell.raw_value
                values[role] = None if raw is None or str(raw).strip() == "" else raw
                if role == "date" and values[role] is not None:
                    values[role] = _day(values[role], cell, epoch)
            if values.get("receipt") is not None:
                rows.append(values)
        tables.append({"roles": set(roles), "rows": rows})
    return tables


def _day(raw, cell, epoch: str | None) -> str:
    """ISO day of a source date cell; a workbook serial number uses the workbook's epoch."""
    if cell is not None and cell.native_type in {"numeric", "integer"} and epoch:
        return parse_date(raw, date_system=epoch).isoformat()
    return parse_date(raw).isoformat()


def _receipts(tables: list[dict]) -> dict[str, dict]:
    """Receipt id -> {date, status, supplier, total}; one receipt table only."""
    heads = [
        t for t in tables if {"receipt", "date"} <= t["roles"] and "quantity" not in t["roles"]
    ]
    lines = [t for t in tables if {"receipt", "date", "quantity"} <= t["roles"]]
    found = heads or lines
    if len(found) != 1:
        raise ContractError("purchase_receipt_table_ambiguous")
    receipts: dict[str, dict] = {}
    for row in found[0]["rows"]:
        key = str(row["receipt"]).strip()
        status = plain(str(row["status"])) if row.get("status") is not None else None
        if status is None and row.get("note") is not None:
            if re.search(r"\bhuy\b", plain(str(row["note"]))):
                raise ContractError("cancelled_receipt_without_status")
        if status is not None and status not in PURCHASE_RECEIVED | PURCHASE_CANCELLED:
            raise ContractError("purchase_status_unknown")
        record = {
            "date": row["date"],
            "cancelled": status in PURCHASE_CANCELLED,
            "supplier": row.get("supplier"),
        }
        old = receipts.get(key)
        if old and (old["date"], old["cancelled"]) != (record["date"], record["cancelled"]):
            raise ContractError("purchase_receipt_conflict")
        receipts[key] = record
    return receipts


def _number(raw) -> Decimal:
    """Exact amount or quantity; consistent groups of three digits are thousands."""
    from src.data.normalization.values import ParseError, parse_money

    if isinstance(raw, bool):
        raise ContractError("purchase_number_invalid")
    if isinstance(raw, int | Decimal):
        return Decimal(raw)
    if isinstance(raw, float):
        if not raw.is_integer():
            raise ContractError("purchase_number_not_exact")
        return Decimal(int(raw))
    token = re.sub(r"\s*(?:đ|d|vnd|dong)$", "", str(raw).strip().casefold())
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+|\d{1,3}(?:\.\d{3})+", token):
        return Decimal(re.sub(r"[.,]", "", token))
    # "12,345tr": before a million/thousand suffix the comma can only mark the fraction.
    convention = "decimal_comma" if re.fullmatch(r"\d+,\d+\s*(?:tr|trieu|k)", token) else None
    try:
        return Decimal(str(parse_money(token, convention)))
    except ParseError as exc:
        raise ContractError("purchase_number_invalid") from exc


def answer(question, context: BusinessContext, text: str):
    """(op, value, type, details) for a weighted purchase price or top supplier."""
    from src.agent.plan import _literal_windows
    from src.agent.rule_ops import _decision, _money, _window

    if not re.search(r"\bnhap\b", text) or not context.prepared:
        return None
    rules = context.rules.rules if context.rules is not None else {}
    average = re.search(r"\bbinh quan\b", text) and re.search(r"\bgia\b|\btien\b", text)
    supplier = re.search(r"\b(?:nha cung cap|ncc) nao\b", text) and re.search(
        r"\b(?:nhieu (?:tien )?(?:hang )?nhat|cao nhat|lon nhat)\b", text
    )
    if re.search(r"\bnhap\b.*\btru\b.*\bban\b|\bchenh lech nhap\b", text):
        return _purchase_minus_sales(question, context, text, rules)
    total_cue = re.search(
        r"\b(?:tong (?:tien|gia tri)|tien (?:hang )?nhap|het (?:bao nhieu|bn|bnhiu) tien|"
        r"bo ra (?:bao nhieu|bn|bnhiu) tien)\b",
        text,
    ) and not re.search(
        r"\b(?:binh quan|trung binh|nha cung cap|ncc|tru|ban|mat hang|nhom|lo|han)\b", text
    )
    if total_cue and not average and not supplier:
        with closing(context.prepared.connect()) as conn:
            if conn.execute("SELECT 1 FROM purchases LIMIT 1").fetchone():
                return None  # receipts read into facts: purchase_total answers
        if "purchase_amount" not in rules or len(_literal_windows(question)) != 1:
            return None
        check = _window(question, context)
        if check is None:
            return None
        if isinstance(check, tuple):
            return _decision("purchase", check, None, "number")
        start, end = check["effective_window"]["start"], check["effective_window"]["end"]
        tables = _tables(context)
        receipts = _receipts(tables)
        keep = {k for k, r in receipts.items() if start <= r["date"] <= end and not r["cancelled"]}
        sources = [t for t in tables if {"receipt", "total"} <= t["roles"]]
        if len(sources) != 1 or not keep:
            return None
        total = Decimal(0)
        for row in sources[0]["rows"]:
            if str(row["receipt"]).strip() in keep:
                if row.get("total") is None:
                    raise ContractError("purchase_amount_missing")
                total += _number(row["total"])
        return _decision("purchase_book_total", check, _money(total), "number")
    if average:
        if "weighted_purchase_price" not in rules:
            return None
        unit = re.search(r"\b(?:mot|1) ([a-z0-9]+)\b", text)
        if not unit:
            return None
    elif supplier:
        if "purchase_amount" not in rules or re.search(r"\b(?:so phieu|chinh|uy tin)\b", text):
            return None
    else:
        return None
    if len(_literal_windows(question)) != 1:
        return None
    check = _window(question, context)
    if check is None:
        return None
    if isinstance(check, tuple):
        return _decision("purchase", check, None, "number" if average else "string")
    start, end = check["effective_window"]["start"], check["effective_window"]["end"]
    tables = _tables(context)
    receipts = _receipts(tables)
    in_period = {k for k, r in receipts.items() if start <= r["date"] <= end and not r["cancelled"]}
    if supplier:
        totals: dict[str, Decimal] = {}
        heads = [t for t in tables if {"receipt", "date", "total", "supplier"} <= t["roles"]]
        if len(heads) != 1:
            return None
        names = _supplier_names(context)
        for row in heads[0]["rows"]:
            key = str(row["receipt"]).strip()
            if key not in in_period:
                continue
            if row.get("supplier") is None or row.get("total") is None:
                raise ContractError("purchase_supplier_or_amount_missing")
            who = str(row["supplier"]).strip()
            who = names.get(who, who) if names else who
            totals[who] = totals.get(who, Decimal(0)) + _number(row["total"])
        if not totals:
            return None
        top = max(totals.values())
        leaders = [k for k, v in totals.items() if v == top]
        if len(leaders) != 1:
            return None
        # Scoring accepts the shop's name or its code (rulebook "Chuỗi").
        return _decision("purchase_top_supplier", check, leaders[0], "string")
    lines = [
        t
        for t in tables
        if {"receipt", "quantity", "unit", "price"} <= t["roles"]
        and ({"code", "name"} & t["roles"])
    ]
    if len(lines) != 1:
        return None
    product = _product(context, text, lines[0])
    if product is None:
        return None
    field, wanted = product
    asked = unit[1]
    quantity = amount = Decimal(0)
    for row in lines[0]["rows"]:
        if str(row["receipt"]).strip() not in in_period or row.get(field) is None:
            continue
        name = str(row[field]).strip() if field == "code" else plain(str(row[field]))
        if name != wanted:
            continue
        if row.get("unit") is None or plain(str(row["unit"])) != asked:
            return None  # another purchase unit would need a conversion
        q, p = _number(row["quantity"]), _number(row["price"])
        quantity += q
        amount += q * p
    if not quantity:
        return None
    return _decision("purchase_average_price", check, _money(amount / quantity), "number")


def _supplier_names(context: BusinessContext) -> dict[str, str]:
    """Supplier code -> name from a supplier list (code and name columns), else {}."""
    for profile in context.catalog["tables"]:
        columns = {c["position"]: _header(c["display_name"]) for c in profile.get("columns", [])}
        code = [p for p, n in columns.items() if re.fullmatch(r"ma (?:nha cung cap|ncc)", n)]
        name = [
            p for p, n in columns.items() if re.fullmatch(r"ten(?: goi| nha cung cap| ncc)?", n)
        ]
        if len(code) == 1 and len(name) == 1 and len(columns) <= 8:
            result = {}
            for row in context.source_table(profile["source_id"]).iter_rows():
                c, n = row.cells.get(code[0]), row.cells.get(name[0])
                if c is not None and n is not None and c.raw_value and n.raw_value:
                    result[str(c.raw_value).strip()] = str(n.raw_value).strip()
            if result:
                return result
    return {}


def _product(context: BusinessContext, text: str, table: dict):
    """("code", product code) or ("name", plain name) of the one product the question names."""
    with closing(context.prepared.connect()) as conn:
        rows = conn.execute("SELECT source_code,name FROM entities WHERE kind='product'").fetchall()
    named = {plain(n): c for c, n in rows if n and len(plain(n).split()) >= 2}
    found = [n for n in named if re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", text)]
    found = [n for n in found if not any(n != m and n in m for m in found)]
    if len(found) != 1:
        return None
    if "code" in table["roles"]:
        code = named[found[0]]
        return ("code", str(code).strip()) if code else None
    return "name", found[0]


def _purchase_minus_sales(question, context: BusinessContext, text: str, rules: dict):
    """§5 "Nhập trừ bán": quantity bought minus quantity sold on §1 orders in the period,
    both in base units, answered in the unit asked (the base unit or a listed large unit)."""
    from src.agent.plan import _literal_windows
    from src.agent.rule_ops import _base_quantity, _decision, _lines, _window
    from src.data.normalization.entities import conversion_table
    from src.data.normalization.stock import identity_text

    if "purchase_sales_quantity" not in rules or len(_literal_windows(question)) != 1:
        return None
    asked = re.findall(r"\b(?:bao nhieu|bn|bnhiu|may|nhieu) ([a-z]+)\b", text)
    if not asked:
        return None
    check = _window(question, context)
    if check is None:
        return None
    if isinstance(check, tuple):
        return _decision("purchase_minus_sales", check, None, "number")
    tables = _tables(context)
    lines = [
        t
        for t in tables
        if {"receipt", "quantity", "unit"} <= t["roles"] and ({"code", "name"} & t["roles"])
    ]
    if len(lines) != 1:
        return None
    product = _product(context, text, lines[0])
    if product is None:
        return None
    field, wanted = product
    with closing(context.prepared.connect()) as conn:
        names = {
            plain(n): (c, u)
            for c, n, u in conn.execute(
                "SELECT source_code,name,base_unit FROM entities WHERE kind='product'"
            )
        }
    name = next(
        (n for n, (c, _) in names.items() if (str(c).strip() if field == "code" else n) == wanted),
        None,
    )
    if name is None or not names[name][1]:
        return None
    base_unit = identity_text(names[name][1])
    conversions = conversion_table(context)
    larges = {}
    if conversions:
        listed = conversions[0].get(identity_text(name))
        if listed:
            if listed[0] != base_unit:
                return None
            larges = listed[1]

    if field == "code":
        larges = {**_unit_factors(context).get(wanted, {}), **larges}

    def bare(unit) -> str:  # question words carry no accents
        return plain(identity_text(str(unit))).replace(" ", "")

    bare_larges = {bare(u): r for u, r in larges.items()}

    def factor(unit) -> Decimal | None:
        unit = bare(unit)
        return Decimal(1) if unit == bare(base_unit) else bare_larges.get(unit)

    start, end = check["effective_window"]["start"], check["effective_window"]["end"]
    receipts = _receipts(tables)
    keep = {k for k, r in receipts.items() if start <= r["date"] <= end and not r["cancelled"]}
    bought = Decimal(0)
    for row in lines[0]["rows"]:
        if str(row["receipt"]).strip() not in keep or row.get(field) is None:
            continue
        key = str(row[field]).strip() if field == "code" else plain(str(row[field]))
        if key != wanted:
            continue
        ratio = factor(row.get("unit") or "")
        if ratio is None:
            return None
        bought += _number(row["quantity"]) * ratio
    sold = Decimal(0)
    for row in _lines(context, check):
        if row[3] == "sale_item" and row[5] and plain(row[5]) == name:
            sold += _base_quantity(row[7], row[8], row[5], row[12])
    ratio = factor(asked[-1])
    if ratio is None:
        return None
    value = (bought - sold) / ratio
    if value != value.to_integral_value():
        return None
    return _decision("purchase_minus_sales", check, int(value), "number")


def _unit_factors(context: BusinessContext) -> dict[str, dict[str, Decimal]]:
    """Product code -> {unit: base units in one} from a per-unit list ("Mã hàng, ĐVT, Quy
    đổi", "MaHangHoa, DonViTinh, HeSoQuyDoi"), else {}; a unit with two factors is dropped."""
    from src.data.normalization.stock import identity_text

    roles = {
        "code": r"ma hang(?: hoa)?|ma sp",
        "unit": r"dvt|don vi tinh|don vi",
        "factor": r"(?:he so )?quy doi|he so",
    }
    found = []
    for profile in context.catalog["tables"]:
        cols: dict[str, list[int]] = {}
        for column in profile.get("columns", []):
            for role, pattern in roles.items():
                if re.fullmatch(pattern, _header(column["display_name"])):
                    cols.setdefault(role, []).append(column["position"])
        if set(cols) == set(roles) and all(len(v) == 1 for v in cols.values()):
            if len(profile.get("columns", [])) <= 6:
                found.append((profile, {k: v[0] for k, v in cols.items()}))
    if len(found) != 1:
        return {}
    profile, at = found[0]
    header = set(profile.get("header_rows", []))
    result: dict[str, dict[str, Decimal]] = {}
    bad: set[tuple[str, str]] = set()
    for row in context.source_table(profile["source_id"]).iter_rows():
        if row.row in header or (header and row.row is not None and row.row < min(header)):
            continue
        cells = {k: row.cells.get(p) for k, p in at.items()}
        if any(c is None or c.raw_value in {None, ""} for c in cells.values()):
            continue
        try:
            value = _number(cells["factor"].raw_value)
        except ContractError:
            continue
        code = str(cells["code"].raw_value).strip()
        unit = identity_text(str(cells["unit"].raw_value))
        if result.setdefault(code, {}).setdefault(unit, value) != value:
            bad.add((code, unit))
    for code, unit in bad:
        result[code].pop(unit, None)
    return result
