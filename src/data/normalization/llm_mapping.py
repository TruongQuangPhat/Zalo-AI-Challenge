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
from src.data.profiling import normalized
from src.jsonio import ContractError, dumps, loads

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


def check_proposal(catalog: dict, proposal: object) -> list[str]:
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
        have = set(used)
        required = {
            "order_lines": [{"ordered_at", "status", "line_net_amount", "item_name"}],
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
            errors.append(f"{key}: role {role} lacks required columns")
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
        errors.append("no complete order source")
    if grain == "ledger_no_order_id" and any(
        "order_id" in proposal["tables"][k]["columns"].values()
        for k, r in roles.items()
        if r == "order_lines"
    ):
        errors.append("ledger grain contradicts an order_id column")
    return errors


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


def verify_facts(connection: sqlite3.Connection, summary: dict, end_hint: str | None) -> list[str]:
    """Extra checks for model-mapped facts; train README-mapped shops all pass them."""
    errors = []
    if summary.get("status") != "ready" or summary.get("blocking_metrics"):
        errors.append(f"facts not ready: blocking {summary.get('blocking_metrics')}")
    if summary.get("issue_counts"):
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
    return errors


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
