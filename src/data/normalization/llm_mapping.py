"""Turn a model's column-meaning proposal into README declarations the fixed mappers read.

Only used after every README mapper failed. The model never touches data or answers: it
labels each column from a closed vocabulary, code writes the matching canonical README
lines, and the unchanged mappers, builders and quality gates decide. A proposal that does
not survive those checks plus ``verify_facts`` leaves the business unsupported.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path

from src.data.cache import fingerprint
from src.data.normalization.values import STATUSES
from src.data.profiling import normalized
from src.jsonio import ContractError, dumps, loads
from src.semantics.rulebook import plain

Propose = Callable[[str, dict], dict]
"""(prompt, JSON schema) -> parsed JSON object; may set a ``model_id`` attribute."""

# key -> (canonical README phrase understood by sales/relational/entities, prompt hint).
MEANINGS: dict[str, tuple[str, str]] = {
    "order_id": ("mã đơn", "mã đơn/hoá đơn/phiếu bán; các dòng hàng của một đơn chung mã"),
    "ordered_at": ("ngày bán", "ngày (hoặc ngày giờ) bán/lập đơn"),
    "status": ("trạng thái đơn", "trạng thái đơn: hoàn thành, huỷ, trả hàng, đang giao…"),
    "line_net_amount": (
        "thành tiền sau giảm",
        "tiền của một dòng hàng sau giảm giá dòng (số lượng × đơn giá − giảm giá dòng)",
    ),
    "item_name": ("tên hàng", "tên mặt hàng/sản phẩm/món/dịch vụ"),
    "quantity": ("số lượng", "số lượng bán của dòng, theo đơn vị của dòng"),
    "product_id": ("mã hàng", "mã mặt hàng/SKU"),
    "customer_id": ("mã khách", "mã khách hàng"),
    "customer_name": ("tên khách (Khách lẻ nếu không lưu)", "tên khách hàng"),
    "customer_group": ("nhóm khách", "nhóm/loại/hạng khách hàng"),
    "item_group": ("nhóm hàng", "nhóm/danh mục/loại mặt hàng"),
    "channel": ("kênh bán", "kênh bán: tại quầy, online, sàn, giao hàng…"),
    "created_at": ("thời điểm tạo đơn", "ngày giờ tạo đơn có giờ phút"),
    "returned_at": ("ngày trả hàng", "ngày khách trả hàng"),
    "returned_quantity": (
        "số lượng khách trả lại (đơn vị cơ bản); lớn hơn 0: đơn đó có trả hàng",
        "số lượng khách trả lại",
    ),
    "order_merchandise_amount": (
        "tiền hàng của đơn: cộng thành tiền các mặt hàng, trước giảm giá của đơn, "
        "chưa gồm phí giao",
        "tổng tiền hàng của cả đơn, trước giảm giá đơn, chưa gồm phí giao",
    ),
    "order_payable": ("tổng tiền đơn", "số khách phải trả cho cả đơn"),
    "order_note": ("ghi chú đơn", "ghi chú của đơn"),
    "unit": ("đơn vị tính", "đơn vị bán của dòng"),
    "base_unit": ("đơn vị cơ bản", "đơn vị nhỏ nhất"),
    "large_unit": ("đơn vị lớn (thùng, hộp, tấn...)", "đơn vị lớn trong bảng quy đổi"),
    "conversion_factor": (
        "hệ số: số đơn vị cơ bản trong 1 đơn vị lớn",
        "số đơn vị cơ bản trong 1 đơn vị lớn",
    ),
    "base_quantity": ("số lượng đã đổi ra đơn vị cơ bản", "số lượng đã quy ra đơn vị cơ bản"),
    "payment_method": (
        "hình thức thanh toán của đơn: cash tiền mặt, transfer chuyển khoản, "
        "ewallet ví điện tử, cod giao hàng thu tiền, debt ghi nợ",
        "hình thức thanh toán của đơn: tiền mặt, chuyển khoản, ví điện tử, COD, ghi nợ",
    ),
    "payment_id": ("mã phiếu thu", "mã phiếu thu tiền"),
    "paid_at": ("thời điểm thu", "ngày giờ thu tiền"),
    "paid_amount": ("số tiền", "số tiền thu trong một lần trả"),
    # Decoys: other money/text columns, so they are not forced into the fields above.
    "unit_price": ("đơn giá", "giá một đơn vị"),
    "line_gross_amount": ("thành tiền trước giảm giá", "tiền dòng trước giảm giá"),
    "discount": ("giảm giá", "tiền/phần trăm giảm giá"),
    "cost_price": ("giá vốn", "giá vốn"),
    "other": ("thông tin khác", "cột khác hoặc không chắc chắn"),
}
DECOYS = {"unit_price", "line_gross_amount", "discount", "cost_price", "other"}
ROLES = {
    "order_lines": "mỗi dòng một dòng hàng của đơn (có ngày, trạng thái, tiền dòng)",
    "order_headers": "mỗi dòng một đơn (mã đơn, ngày, trạng thái)",
    "order_details": "chi tiết đơn, nối với bảng đơn qua mã đơn",
    "products": "danh mục hàng",
    "customers": "danh sách khách",
    "unit_conversion": "bảng quy đổi đơn vị",
    "payments": "các lần khách trả tiền cho đơn",
    "other": "bảng phụ không chứa giao dịch bán (nhập hàng, tồn kho, nhân viên…)",
}
DECLARATIONS = {
    "row_grain": {
        "order_line": "Mỗi dòng một dòng hàng.",
        "order_header_only": "Mỗi dòng một đơn hàng, không có chi tiết mặt hàng.",
        "ledger_no_order_id": "Mỗi dòng một dòng hàng, không ghi mã đơn.",
    },
    "order_info": {
        "repeated_every_row": "Thông tin đơn lặp lại ở mọi dòng.",
        "first_row_of_group_only": "Thông tin đơn chỉ ghi ở dòng đầu nhóm.",
        "separate_tables": "",
    },
    "numeric_convention": {
        "dot_grouped": "Số tiền dùng dấu chấm nhóm nghìn.",
        "comma_grouped": "Số tiền dùng dấu phẩy nhóm nghìn.",
        "decimal_dot": "Số tiền dùng dấu chấm thập phân, không nhóm nghìn.",
        "decimal_comma": "Số tiền dùng dấu phẩy thập phân, không nhóm nghìn.",
        "unknown": "",
    },
    "multi_part": {True: "Dữ liệu bán hàng chia nhiều file hoặc mỗi tháng một sheet.", False: ""},
    "negative_quantity_is_return": {True: "Số lượng âm là trả hàng.", False: ""},
    "exported_from_sales_software": {True: "File xuất từ phần mềm bán hàng.", False: ""},
}
AUXILIARY = "File phụ, không chứa giao dịch bán."
_ORDER_ROLES = {"order_lines", "order_headers", "order_details"}
_SECTION = re.compile(r"^## Cột trong `([^`]+)`(?:, sheet `([^`]+)`)?\s*$")
_BULLET = re.compile(r"^(\s*-\s+\*\*)(.+?)(\*\*\s*:\s*)(.+)$")


def _tables(catalog: dict) -> list[dict]:
    return [
        t
        for t in catalog["tables"]
        if t["read_status"] in {"read", "empty"} and t["structure_status"] == "resolved"
    ]


def _groups(catalog: dict) -> dict[str, list[dict]]:
    """Proposal keys: sheets of one file with identical columns share one entry."""
    by_shape: dict[tuple, list[dict]] = {}
    for table in _tables(catalog):
        name = Path(table["source_ref"]["file"]).name
        shape = tuple(c["display_name"] for c in table["columns"])
        by_shape.setdefault((name, shape if table["format"] != "sqlite" else id(table)), []).append(
            table
        )
    groups: dict[str, list[dict]] = {}
    for (name, _), tables in by_shape.items():
        first = tables[0]
        if first["format"] == "csv":
            key = name
        elif len(tables) == 1:
            key = f"{name} / {first['source_ref']['table']}"
        else:
            key = f"{name} / {first['source_ref']['table']} (+{len(tables) - 1} sheet cùng cột)"
        if key in groups:
            raise ContractError("duplicate table names cannot be addressed by a proposal")
        groups[key] = tables
    return groups


def _column_keys(table: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for column in table["columns"]:
        counts[column["display_name"]] = counts.get(column["display_name"], 0) + 1
    return {
        (c["display_name"] if counts[c["display_name"]] == 1 else f"{c['display_name']} #{p}"): p
        for c in table["columns"]
        for p in [c["position"]]
    }


def build_request(catalog: dict, feedback: list[str] | None = None) -> tuple[str, dict]:
    groups = _groups(catalog)
    keys = list(groups)
    tables = [group[0] for group in groups.values()]
    blocks = []
    for key, group in groups.items():
        table = group[0]
        count = sum(t.get("raw_row_count") or 0 for t in group)
        rows = [f"### `{key}` ({count} dòng)"]
        for label, position in _column_keys(table).items():
            column = next(c for c in table["columns"] if c["position"] == position)
            samples = [str(s["raw_value"])[:30] for s in column.get("samples", [])[:3]]
            rows.append(f"- {json.dumps(label, ensure_ascii=False)}: {samples}")
        blocks.append("\n".join(rows))
    meanings = "\n".join(f"- {k}: {hint}" for k, (_, hint) in MEANINGS.items())
    roles = "\n".join(f"- {k}: {hint}" for k, hint in ROLES.items())
    prompt = (
        "Bạn gán ý nghĩa cho các cột dữ liệu bán hàng của một cửa hàng Việt Nam.\n"
        "Với mỗi bảng, chọn vai trò; với mỗi cột, chọn đúng một nhãn ý nghĩa. "
        "Chỉ chọn nhãn khi tên cột, README và giá trị mẫu cùng ủng hộ; không chắc thì chọn "
        "`other`. Cột tiền chỉ là `line_net_amount` khi đó là tiền của một dòng hàng đã trừ "
        "giảm giá dòng; đơn giá, tiền trước giảm, tổng tiền đơn phải dùng nhãn riêng.\n\n"
        f"Vai trò bảng:\n{roles}\n\nNhãn cột:\n{meanings}\n\n"
        f"README của cửa hàng:\n<<<\n{catalog['readme'].strip()}\n>>>\n\n"
        "Các bảng (tên cột: giá trị mẫu):\n\n" + "\n\n".join(blocks)
    )
    if feedback:
        prompt += "\n\nĐề xuất trước bị từ chối vì:\n" + "\n".join(f"- {e}" for e in feedback)
    table_schema = {
        key: {
            "type": "object",
            "properties": {
                "role": {"enum": list(ROLES)},
                "columns": {
                    "type": "object",
                    "properties": {c: {"enum": list(MEANINGS)} for c in _column_keys(t)},
                    "required": list(_column_keys(t)),
                    "additionalProperties": False,
                },
            },
            "required": ["role", "columns"],
            "additionalProperties": False,
        }
        for key, t in zip(keys, tables, strict=True)
    }
    schema = {
        "type": "object",
        "properties": {
            "tables": {
                "type": "object",
                "properties": table_schema,
                "required": keys,
                "additionalProperties": False,
            },
            "declarations": {
                "type": "object",
                "properties": {k: {"enum": list(v)} for k, v in DECLARATIONS.items()},
                "required": list(DECLARATIONS),
                "additionalProperties": False,
            },
        },
        "required": ["tables", "declarations"],
        "additionalProperties": False,
    }
    return prompt, schema


def check_proposal(catalog: dict, proposal: object, note_status: bool = False) -> list[str]:
    """Schema conformance plus role/column consistency; empty list means usable."""
    tables = {key: group[0] for key, group in _groups(catalog).items()}
    if not isinstance(proposal, dict) or not isinstance(proposal.get("tables"), dict):
        return ["proposal must be an object with tables"]
    errors = []
    declarations = proposal.get("declarations")
    if not isinstance(declarations, dict) or set(declarations) != set(DECLARATIONS):
        return ["declarations must list every declaration"]
    for key, allowed in DECLARATIONS.items():
        if declarations[key] not in allowed:
            errors.append(f"declaration {key} has invalid value")
    if set(proposal["tables"]) != set(tables):
        return errors + ["tables must list every readable table exactly once"]
    order_columns: dict[str, frozenset] = {}
    roles: dict[str, str] = {}
    for key, entry in proposal["tables"].items():
        columns = entry.get("columns") if isinstance(entry, dict) else None
        role = entry.get("role") if isinstance(entry, dict) else None
        if role not in ROLES or not isinstance(columns, dict):
            errors.append(f"{key}: invalid role or columns")
            continue
        if set(columns) != set(_column_keys(tables[key])) or not all(
            v in MEANINGS for v in columns.values()
        ):
            errors.append(f"{key}: columns must label every column from the vocabulary")
            continue
        roles[key] = role
        used = [v for v in columns.values() if v not in DECOYS]
        for meaning in sorted({m for m in used if used.count(m) > 1}):
            errors.append(f"{key}: meaning {meaning} assigned to several columns")
        have = _with_note_status(set(used), note_status)
        required = {
            "order_lines": [{"ordered_at", "status", "line_net_amount", "item_name"}]
            + ([{"ordered_at", "status", "line_net_amount"}] if "order_id" not in have else []),
            "order_headers": [{"order_id", "ordered_at", "status"}],
            "order_details": [
                {"order_id", "line_net_amount", "product_id"},
                {"order_id", "line_net_amount", "item_name"},
            ],
            "products": [{"product_id"}, {"item_name"}],
            "customers": [{"customer_id"}, {"customer_name"}],
            "unit_conversion": [{"conversion_factor"}],
            "payments": [{"order_id", "paid_amount"}],
            "other": [set()],
        }[role]
        if (
            role == "order_lines"
            and declarations.get("row_grain") != "ledger_no_order_id"
            and "order_id" not in have
        ):
            errors.append(f"{key}: order lines need order_id unless the grain is a ledger")
        if not any(r <= have for r in required):
            missing = sorted(min((r - have for r in required), key=len))
            errors.append(f"{key}: role {role} lacks required columns {missing}")
        if role not in _ORDER_ROLES and have & {"status", "line_net_amount"}:
            errors.append(f"{key}: role {role} cannot carry order status or line amounts")
        names = frozenset(normalized(c).strip() for c in columns)
        if role in _ORDER_ROLES:
            order_columns[key] = names
    for key, role in roles.items():
        names = frozenset(normalized(c).strip() for c in proposal["tables"][key]["columns"])
        if role not in _ORDER_ROLES and names in order_columns.values():
            errors.append(f"{key}: same columns as an order table cannot be auxiliary")
    present = set(roles.values())
    grain = declarations.get("row_grain")
    if not ("order_lines" in present or {"order_headers", "order_details"} <= present) and not (
        "order_headers" in present and grain == "order_header_only"
    ):
        errors.append(
            "no complete order source: cần một bảng order_lines (ordered_at, status, "
            "line_net_amount, item_name), hoặc order_headers (order_id, ordered_at, status) "
            "cùng order_details (order_id, line_net_amount, product_id hoặc item_name)"
        )
    if grain == "ledger_no_order_id" and any(
        "order_id" in proposal["tables"][k]["columns"].values()
        for k, r in roles.items()
        if r == "order_lines"
    ):
        errors.append("ledger grain contradicts an order_id column")
    return errors


_CORE = {"order_id", "ordered_at", "status", "line_net_amount", "item_name", "product_id"}


def _with_note_status(have: set[str], note_status: bool) -> set[str]:
    """Rulebook "Sổ không có cột trạng thái": the order note stands in for the status."""
    return have | {"status"} if note_status and "order_note" in have else have


def _order_role(have: set[str], note_status: bool = False) -> str | None:
    have = _with_note_status(have, note_status)
    if {"ordered_at", "status", "line_net_amount", "item_name"} <= have:
        return "order_lines"
    # Rulebook "Bảng không ghi theo đơn": without order IDs each row is one record, so a row
    # amount needs no item name (a repair-ticket book: one ticket per row).
    if {"ordered_at", "status", "line_net_amount"} <= have and "order_id" not in have:
        return "order_lines"
    if {"order_id", "ordered_at", "status"} <= have:
        return "order_headers"
    if {"order_id", "line_net_amount"} <= have and have & {"product_id", "item_name"}:
        return "order_details"
    return None


def _order_level(readme: str, header: str) -> bool:
    """README says the column is written for the whole order ("Bớt: tiền bớt cho cả đơn",
    "khách, bớt, ghi chú của đơn lặp lại ở mọi dòng")."""
    word = re.escape(normalized(header).strip())
    for sentence in re.split(r"[.\n]", normalized(readme)):
        if re.search(r"(?<!\w)" + word + r"(?!\w)", sentence) and re.search(
            r"\b(?:cho|cua) ca don\b|\bcua don lap lai\b", sentence
        ):
            return True
    return False


def normalize_proposal(catalog: dict, proposal: dict, note_status: bool = False) -> dict:
    """Derive what code can establish itself; the model's column labels stay the evidence.

    - An order role is re-derived from the labels (lines/headers/details). A table the model
      called an order table that fits none becomes ``other`` when another order source
      exists; a non-order table keeps its role but loses order status/line amounts, and a
      product/customer list without both code and name becomes auxiliary.
    - A non-core label on several columns is dropped from all of them (core stays rejected).
    - Grain, order-info layout and multi-part come from the labels and source profiles;
      the numeric convention is left to source evidence.
    Structurally invalid proposals are returned unchanged for ``check_proposal`` to reject.
    """
    groups = _groups(catalog)
    tables = proposal.get("tables") if isinstance(proposal, dict) else None
    if (
        not isinstance(tables, dict)
        or set(tables) != set(groups)
        or not isinstance(proposal.get("declarations"), dict)
        or not all(
            isinstance(e, dict)
            and e.get("role") in ROLES
            and isinstance(e.get("columns"), dict)
            and set(e["columns"]) == set(_column_keys(groups[k][0]))
            and all(v in MEANINGS for v in e["columns"].values())
            for k, e in tables.items()
        )
    ):
        return proposal
    result = {}
    for key, entry in tables.items():
        columns = dict(entry["columns"])
        labels = set(columns.values())
        # One row = one record (no order IDs): the row's whole amount is its line amount.
        if (
            "order_id" not in labels
            and "ordered_at" in labels
            and "line_net_amount" not in labels
            and _with_note_status(labels, note_status) >= {"status"}
        ):
            amounts = [
                c for c, m in columns.items() if m in {"order_payable", "order_merchandise_amount"}
            ]
            if len(amounts) == 1:
                columns[amounts[0]] = "line_net_amount"
        # Rulebook §1: an order's revenue never subtracts an order-level discount. When the
        # only discount is the order's (README: written for the whole order), the line
        # amount before it is the line's revenue amount.
        gross = [c for c, m in columns.items() if m == "line_gross_amount"]
        readme = catalog.get("readme") or ""
        # A "line amount" the README writes for the whole order is the order's payable.
        for column, meaning in list(columns.items()):
            if meaning == "line_net_amount" and len(gross) == 1 and _order_level(readme, column):
                columns[column] = "order_payable"
        labels = set(columns.values())
        if (
            "order_id" in labels
            and "line_net_amount" not in labels
            and len(gross) == 1
            and all(_order_level(readme, c) for c, m in columns.items() if m == "discount")
        ):
            columns[gross[0]] = "line_net_amount"
        # A header that says "(ĐV cơ bản)" counts base units: it is the base quantity, and an
        # unlabelled plain quantity header ("S.L", "SL") is then the line's own quantity.
        base_named = [
            c for c, m in columns.items() if m == "quantity" and re.search(r"\bco ban\b", plain(c))
        ]
        plain_qty = [
            c
            for c, m in columns.items()
            if m == "other" and re.fullmatch(r"s\.?\s?l\.?|so luong", plain(c).strip())
        ]
        if len(base_named) == 1 and len(plain_qty) == 1 and "base_quantity" not in labels:
            columns[base_named[0]] = "base_quantity"
            columns[plain_qty[0]] = "quantity"
        used = [v for v in columns.values() if v not in DECOYS]
        for label, meaning in columns.items():
            if meaning not in _CORE and used.count(meaning) > 1:
                columns[label] = "other"
        have = set(columns.values())
        role = entry["role"]
        if role in _ORDER_ROLES:
            role = _order_role(have, note_status) or role
        else:
            columns = {
                c: "other" if m in {"status", "line_net_amount"} else m for c, m in columns.items()
            }
            # Masters the relational join cannot key (code and name) are kept as auxiliary.
            pair = {
                "products": {"product_id", "item_name"},
                "customers": {"customer_id", "customer_name"},
            }
            if role in pair and not pair[role] <= set(columns.values()):
                role = "other"
        result[key] = {"role": role, "columns": columns}
    complete = [
        k
        for k, e in result.items()
        if e["role"] in _ORDER_ROLES
        and _order_role(set(e["columns"].values()), note_status) == e["role"]
    ]
    for key, entry in result.items():
        if entry["role"] in _ORDER_ROLES and key not in complete and complete:
            entry["role"] = "other"
            entry["columns"] = {
                c: "other" if m in {"status", "line_net_amount"} else m
                for c, m in entry["columns"].items()
            }
    lines = [k for k, e in result.items() if e["role"] == "order_lines"]
    headers = [k for k, e in result.items() if e["role"] == "order_headers"]
    details = [k for k, e in result.items() if e["role"] == "order_details"]
    if any("order_id" not in result[k]["columns"].values() for k in lines):
        grain = "ledger_no_order_id"
    elif headers and not lines and not details:
        grain = "order_header_only"
    else:
        grain = "order_line"
    order_info = "separate_tables"
    if lines:
        sparse = []
        for key in lines:
            position = next(
                _column_keys(groups[key][0])[c]
                for c, m in result[key]["columns"].items()
                if m == "ordered_at"
            )
            empty = rows = 0
            for table in groups[key]:
                column = next(c for c in table["columns"] if c["position"] == position)
                empty += (column.get("null_count") or 0) + (column.get("blank_string_count") or 0)
                rows += column.get("row_count") or 0
            sparse.append(rows > 0 and empty > 0.2 * rows)
        order_info = "first_row_of_group_only" if any(sparse) else "repeated_every_row"
    parts = {
        role: sum(len(groups[k]) for k, e in result.items() if e["role"] == role)
        for role in _ORDER_ROLES
    }
    declarations = {
        **proposal["declarations"],
        "row_grain": grain,
        "order_info": order_info,
        "numeric_convention": "unknown",
        "multi_part": any(n > 1 for n in parts.values()),
    }
    return {"tables": result, "declarations": declarations}


def synthesize_readme(catalog: dict, proposal: dict) -> str:
    """Original README plus canonical meaning bullets and declarations; nothing removed."""
    groups = _groups(catalog)
    per_file: dict[str, int] = {}
    for group in groups.values():
        name = Path(group[0]["source_ref"]["file"]).name
        per_file[name] = per_file.get(name, 0) + 1
    sections: dict[tuple[str, str], tuple[dict[str, str], bool]] = {}
    for key, group in groups.items():
        entry = proposal["tables"][key]
        labelled = (
            {label: entry["columns"][label] for label in _column_keys(group[0])},
            entry["role"] in {"other", "payments"},
        )
        name = Path(group[0]["source_ref"]["file"]).name
        if per_file[name] == 1 and group[0]["format"] != "sqlite":
            sections[(name, "")] = labelled
        else:
            for table in group:
                sections[(name, table["source_ref"]["table"])] = labelled
    lines = catalog["readme"].rstrip("\n").splitlines()
    for (name, sheet), (labels, auxiliary) in sections.items():
        start = next(
            (
                i
                for i, line in enumerate(lines)
                if (m := _SECTION.match(line)) and (m[1], m[2] or "") == (name, sheet)
            ),
            None,
        )
        if start is None:
            heading = f"## Cột trong `{name}`" + (f", sheet `{sheet}`" if sheet else "")
            lines += ["", heading]
            start = len(lines) - 1
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines)
        )
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        body = lines[start + 1 : end]
        positions = {
            normalized(m[2]).strip(): i for i, m in enumerate(map(_BULLET.match, body)) if m
        }
        for label, meaning in labels.items():
            display = re.sub(r" #\d+$", "", label)
            index = positions.get(normalized(display).strip())
            if meaning == "other" and index is not None:
                continue
            bullet = f"- **{display}**: {MEANINGS[meaning][0]}"
            if index is None:
                body.append(bullet)
            else:
                body[index] = bullet
        if auxiliary and AUXILIARY not in body:
            body.append(AUXILIARY)
        lines[start + 1 : end] = body
    declarations = proposal["declarations"]
    sentences = [DECLARATIONS[k][declarations[k]] for k in DECLARATIONS]
    statement = " ".join(s for s in sentences if s)
    if statement:
        title = next((i for i, line in enumerate(lines) if line.startswith("# ")), -1)
        lines[title + 1 : title + 1] = ["", statement]
    return "\n".join(lines) + "\n"


GROUP_ONLY = {"missing_group_reference"}
GROUP_ONLY_METRICS = {"item_group_share"}


def verify_facts(connection: sqlite3.Connection, summary: dict, end_hint: str | None) -> list[str]:
    """Extra checks for model-mapped facts; train README-mapped shops all pass them."""
    errors = []
    # A product missing from the group list blocks only group shares (preflight keeps them
    # blocked); every other issue rejects the mapping.
    blocking = set(summary.get("blocking_metrics") or []) - GROUP_ONLY_METRICS
    issues = {k: v for k, v in (summary.get("issue_counts") or {}).items() if k not in GROUP_ONLY}
    if summary.get("status") not in {"ready", "blocked"} or blocking:
        errors.append(f"facts not ready: blocking {summary.get('blocking_metrics')}")
    if issues:
        errors.append(f"normalization issues: {summary['issue_counts']}")
    rows = connection.execute(
        "SELECT count(*), sum(status='completed'), min(ordered_at), max(ordered_at) FROM orders"
    ).fetchone()
    if not rows[0]:
        rows = connection.execute(
            "SELECT count(*), sum(status='completed'), min(occurred_at), max(occurred_at) "
            "FROM sales_events"
        ).fetchone()
    total, completed, first, last = rows
    if not total:
        return errors + ["no orders or sales events"]
    if completed < 0.5 * total:
        errors.append(f"only {completed}/{total} completed; status mapping is doubtful")
    if first[:10] < "2000-01-01" or (end_hint and last[:10] > end_hint):
        errors.append(f"dates {first[:10]}..{last[:10]} outside the README coverage")
    amounts = [
        row[0]
        for table in ["order_lines", "order_revenues", "sales_events"]
        for row in connection.execute(f"SELECT net_amount FROM {table}")
        if row[0] is not None
    ]
    try:
        values = [Decimal(a) for a in amounts]
    except InvalidOperation:
        return errors + ["unparsed money"]
    if any(v != v.to_integral_value() for v in values):
        errors.append("money is not whole VND")
    if sum(values) <= 0:
        errors.append("non-positive revenue")
    # An order-level amount ("Khách trả", the whole bill) labelled as the line amount repeats
    # on every line of an order; revenue would then multiply by the line count.
    multi, repeated = connection.execute(
        "SELECT count(*), coalesce(sum(n_amounts = 1), 0) FROM ("
        " SELECT count(DISTINCT net_amount) AS n_amounts FROM order_lines"
        " WHERE row_role = 'sale_item' AND net_amount IS NOT NULL"
        " GROUP BY order_key HAVING count(*) >= 2)"
    ).fetchone()
    if multi >= 20 and repeated > 0.5 * multi:
        errors.append(
            f"{repeated}/{multi} multi-line orders repeat one amount on every line; "
            "the column labelled line_net_amount is an order-level amount (order total): "
            "label it order_payable and label the per-line amount column line_net_amount"
        )
    return errors + _price_arithmetic(connection, summary)


def _cell_numbers(source_values: str) -> set[Decimal]:
    from src.data.normalization.values import ParseError, parse_money

    numbers = set()
    for cell in loads(source_values):
        token = re.sub(r"\s*(?:vnd|vnđ|đ|₫)$", "", str(cell[2]).strip().casefold())
        if re.fullmatch(r"-?\d{1,3}(?:[.,]\d{3})+", token):
            token = re.sub(r"[.,]", "", token)
        # "3,9tr", "1200k": either separator may be the decimal mark of a scaled amount.
        for convention in (None, "decimal_comma", "decimal_dot"):
            try:
                numbers.add(parse_money(token, convention))
            except (ParseError, InvalidOperation, ValueError):
                continue
    return numbers


def _price_arithmetic(connection: sqlite3.Connection, summary: dict) -> list[str]:
    """Quantity x unit price (less a discount) must give the line amount.

    Independent of the labels: when the proposal names a unit price on the sale lines, a
    line sold in quantity q > 1 for amount a must carry some cell p with q x p = a, or
    q x p - d = a for a discount cell d. A wrong quantity or amount column breaks this on
    most lines, and every answer of the shop with it.
    """
    tables = ((summary.get("proposal") or {}).get("tables") or {}).values()
    if not any(
        t.get("role") == "order_lines" and "unit_price" in (t.get("columns") or {}).values()
        for t in tables
    ):
        return []
    checked = passed = 0
    for amount, quantity, source_values in connection.execute(
        "SELECT l.net_amount, i.sold_quantity, l.source_values FROM order_lines l "
        "JOIN line_items i USING (line_key) WHERE l.row_role = 'sale_item' "
        "AND l.net_amount IS NOT NULL AND i.sold_quantity IS NOT NULL"
    ):
        quantity, amount = Decimal(quantity), Decimal(amount)
        if quantity in (0, 1):
            continue
        checked += 1
        cells = _cell_numbers(source_values) - {amount}
        gross = {quantity * price for price in cells}
        passed += amount in gross or any(g - d == amount for g in gross for d in cells if 0 < d < g)
    if checked >= 20 and passed < Decimal("0.95") * checked:
        return [
            f"quantity x unit price gives the line amount on only {passed}/{checked} lines; "
            "the quantity or line_net_amount column is mislabelled"
        ]
    return []


def request_proposal(
    catalog: dict, propose: Propose, cache_dir: Path | None, feedback: list[str]
) -> dict:
    """Ask once per (prompt, schema, model); proposals are cached as JSON files."""
    prompt, schema = build_request(catalog, feedback)
    key = fingerprint(
        {"prompt": prompt, "schema": schema, "model": getattr(propose, "model_id", None)}
    )
    path = cache_dir / "llm_mapping" / f"{key}.json" if cache_dir else None
    if path is not None and path.is_file() and not path.is_symlink():
        return loads(path.read_text(encoding="utf-8"))["proposal"]
    proposal = propose(prompt, schema)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"business_id": catalog["business_id"], "proposal": proposal, "prompt": prompt}
        path.write_text(dumps(record) + "\n", encoding="utf-8")
    return proposal


STATUS_CHOICES = {
    "completed": "đơn đã bán xong, tính doanh thu",
    "cancelled": "đơn huỷ, không tính doanh thu",
    "returned": "đơn khách trả hàng",
    "shipping": "đơn đang giao/đang xử lý, chưa huỷ",
    "unknown": "không chắc chắn",
    "not_status": "ghi chú không nói gì về kết quả của đơn (đơn hoàn thành)",
}


def build_status_request(values: list[str]) -> tuple[str, dict]:
    """Classify order-status values the fixed vocabulary does not know (closed enum)."""
    examples = "\n".join(
        f"- {status}: {', '.join(sorted(words)[:8])}" for status, words in STATUSES.items()
    )
    choices = "\n".join(f"- {k}: {hint}" for k, hint in STATUS_CHOICES.items())
    prompt = (
        "Cột trạng thái đơn (hoặc ghi chú đơn, khi sổ không có cột trạng thái) của một cửa "
        "hàng Việt Nam có các giá trị dưới đây. Với mỗi giá trị, chọn đúng một trạng thái "
        "chuẩn. Ghi chú không nói về kết quả của đơn thì chọn `not_status`. Không chắc thì "
        "chọn `unknown`.\n\n"
        f"Trạng thái chuẩn:\n{choices}\n\nVí dụ cách ghi đã biết:\n{examples}\n\n"
        "Các giá trị cần phân loại:\n"
        + "\n".join(f"- {json.dumps(v, ensure_ascii=False)}" for v in values)
    )
    schema = {
        "type": "object",
        "properties": {v: {"enum": list(STATUS_CHOICES)} for v in values},
        "required": values,
        "additionalProperties": False,
    }
    return prompt, schema


def request_status_aliases(
    business_id: str, values: list[str], propose: Propose, cache_dir: Path | None
) -> dict[str, str]:
    """Normalized status token -> canonical status; ``unknown`` answers are left out."""
    prompt, schema = build_status_request(values)
    key = fingerprint(
        {"prompt": prompt, "schema": schema, "model": getattr(propose, "model_id", None)}
    )
    path = cache_dir / "llm_mapping" / f"status-{key}.json" if cache_dir else None
    if path is not None and path.is_file() and not path.is_symlink():
        answer = loads(path.read_text(encoding="utf-8"))["proposal"]
    else:
        answer = propose(prompt, schema)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            record = {"business_id": business_id, "proposal": answer, "prompt": prompt}
            path.write_text(dumps(record) + "\n", encoding="utf-8")
    if not isinstance(answer, dict) or set(answer) != set(values):
        raise ContractError("status classification does not cover the asked values")
    aliases = {}
    for value, status in answer.items():
        if status not in STATUS_CHOICES:
            raise ContractError("status classification outside the closed vocabulary")
        if status != "unknown":
            aliases[normalized(value).strip()] = status
    return aliases
