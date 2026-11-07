"""Bounded entity/business extensions of QueryPlan, never a second query contract."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from src.data.catalog import BusinessContext
from src.data.normalization.stock import _alias_pattern
from src.data.split import Question
from src.jsonio import ContractError, loads
from src.semantics.rulebook import plain

if TYPE_CHECKING:
    from src.data.normalization.store import PreparedBusiness


@lru_cache(maxsize=4)
def _candidate_catalog(path: Path, revision: tuple, kind: str) -> tuple | None:
    """Cache identities/aliases only, at most 4096 rows and 1 MiB text per entry.

    The path plus inode/size/mtime identify the immutable prepared revision. No
    question, as-of, source rows, metrics or query answers enter this cache.
    """
    result, size = [], 0
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        for key, code, name, unit, aliases in conn.execute(
            "SELECT entity_key,source_code,name,base_unit,aliases FROM entities WHERE kind=?",
            (kind,),
        ):
            size += sum(len(v.encode()) for v in [key, code, name, unit, aliases] if v)
            if size > 1024 * 1024 or len(result) >= 4096:
                return None
            result.append((key, code, name, unit, tuple(loads(aliases))))
    if _catalog_revision(path) != revision:
        raise ContractError("entity catalog changed during read")
    return tuple(result)


def _catalog_revision(path: Path) -> tuple:
    info = path.stat()
    return info.st_ino, info.st_size, info.st_mtime_ns


def _candidate_rows(prepared: PreparedBusiness, kind: str) -> Iterator[tuple]:
    path = prepared.path
    if path is None or path.is_symlink():
        raise ContractError("prepared entity catalog unavailable")
    path = path.resolve()
    rows = _candidate_catalog(path, _catalog_revision(path), kind)
    if rows is not None:
        yield from rows
    else:
        with closing(prepared.connect()) as conn:
            for key, code, name, unit, aliases in conn.execute(
                "SELECT entity_key,source_code,name,base_unit,aliases FROM entities WHERE kind=?",
                (kind,),
            ):
                yield key, code, name, unit, tuple(loads(aliases))


@lru_cache(maxsize=4)
def _mention_names(path: Path, revision: tuple) -> tuple[str, ...]:
    """Accentless multi-word product/customer names and item/customer group labels."""
    names = set()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        for name, aliases in conn.execute("SELECT name, aliases FROM entities"):
            names.update(plain(n) for n in (name, *loads(aliases)))
        for table, column in (
            ("line_groups", "item_group"),
            ("event_groups", "item_group"),
            ("order_groups", "customer_group"),
        ):
            try:
                names.update(
                    plain(v) for (v,) in conn.execute(f"SELECT DISTINCT {column} FROM {table}")
                )
            except sqlite3.OperationalError:
                continue
    # Single words ("le", "ca phe") collide with ordinary wording; require two tokens.
    return tuple(sorted(n for n in names if len(n.split()) >= 2))


def mentioned_entity(context: BusinessContext, question: Question) -> str | None:
    """First source entity or group name the question names, accent-insensitively."""
    if not context.prepared or context.prepared.path is None or context.prepared.path.is_symlink():
        return None
    path = context.prepared.path.resolve()
    text = plain(question.question)
    return next(
        (
            name
            for name in _mention_names(path, _catalog_revision(path))
            if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text)
        ),
        None,
    )


def entity_candidates(context: BusinessContext, question: str, kind: str = "product") -> list[dict]:
    if not context.prepared or not context.prepared.summary.get("extensions", {}).get("counts"):
        return []
    # Accent preserving only. Source aliases have provenance in entities.source_ref.
    result, coded = [], []
    for key, code, name, unit, aliases in _candidate_rows(context.prepared, kind):
        row = {"entity_key": key, "source_code": code, "name": name, "base_unit": unit}
        if code and re.search(_alias_pattern(code), question.casefold()):
            coded.append(row)
        if any(
            re.search(_alias_pattern(a), question.casefold()) for a in (*aliases, code or "") if a
        ):
            result.append(row)
    return (coded or result)[:9]


def business_spec(question: Question) -> dict | None:
    """Finite intent guards, only question text/capability; no answers or IDs."""
    from src.agent.plan import _literal_windows

    text = plain(question.question)
    text = re.sub(r"\bthangs\b", "thang", text)
    text = re.sub(r"\bsoos\b", "so", text)
    # A complete bounded paraphrase binds the same customer-money ranking intent.
    # Requiring the full sentence prevents a new route from dropping qualifiers.
    money_paraphrase = re.fullmatch(
        r"(?P<prefix>(?:top [1-9]\d? )?khach(?: hang)?(?: nao)?(?: co)? )"
        r"(?:chi tieu nhieu nhat|doanh thu cao nhat) "
        r"(?P<period>thang nay|thang truoc|hom nay)\s*[?!.]*",
        text,
    )
    if money_paraphrase:
        text = money_paraphrase["prefix"] + "mua nhieu tien nhat " + money_paraphrase["period"]
    nearest_lot = bool(re.search(r"\blo\b.*\bgan het han nhat\b", text))
    if re.search(
        (
            "\\b(ngoai tru|loai tru|nhom|hoat chat|ke don|tuoi no|hon \\d+ ngay|qua \\d+"
            " ngay|no them)\\b"
        ),
        text,
    ):
        return None
    if re.search(
        (
            "\\b(khong tinh|chi tinh|rieng|nhan vien|kenh|ca sang|ca chieu|tien "
            "mat|chuyen khoan|don huy|don tra|tro len|tro xuong)\\b"
        ),
        text,
    ):
        return None
    if "%" in text and not nearest_lot:
        return None
    expressions = _literal_windows(question)
    current_point = all(
        expression in ({"kind": "today"}, {"kind": "day", "date": question.as_of_date})
        for expression in expressions
    )
    if re.search(r"\bdoanh thu co tang lien tuc\b|\bdoanh thu thang sau co luon cao hon\b", text):
        months = re.findall(r"thang (\d{1,2})/(\d{4})", text)
        if len(months) == 2:
            first = int(months[0][1]) * 12 + int(months[0][0]) - 1
            last = int(months[1][1]) * 12 + int(months[1][0]) - 1
            if last - first == 2:
                months = [(str(i % 12 + 1), str(i // 12)) for i in range(first, last + 1)]
        if len(months) == 3:
            return {
                "metric": "revenue",
                "group": None,
                "limit": None,
                "type": "boolean",
                "trend": True,
                "expressions": [
                    {"kind": "month", "month": int(m), "year": int(y)} for m, y in months
                ],
            }
    debt = bool(re.search(r"\b(con no|con nowj|cong no|khach.*no.*nhieu nhat)\b", text))
    collection = bool(re.search(r"\b(tra no|thu hoi|tra bot no)\b", text))
    if (debt or collection) and re.search(
        r"\bkhach (?!hang\b|con\b|no\b|nowj\b|tra\b|mua\b|nao\b|dang\b|chua\b|co\b|nhieu\b|cong\b)",
        text,
    ):
        return None
    if debt and not collection:
        if expressions and (
            len(expressions) != 1 or expressions[0]["kind"] not in {"day", "today"}
        ):
            return None
        if re.search(r"\b(nhieu nhat|lon nhat)\b", text) and re.search(
            r"\b(kem|moi nguoi|tung nguoi|so no)\b", text
        ):
            count = re.search(r"\b([1-9]\d?) khach", text)
            if not count:
                return None
            metric, group, limit, shape = "receivable", "customer", int(count[1]), "table"
        else:
            metric = (
                "receivable_customer_count"
                if re.search(r"\b(so khach|bao nhieu khach|may khach)\b", text)
                else "receivable"
            )
            group, limit, shape = None, None, "number"
        return {
            "metric": metric,
            "group": group,
            "limit": limit,
            "type": shape,
            "expressions": expressions
            if len(expressions) == 1 and expressions[0]["kind"] == "day"
            else [{"kind": "day", "date": question.as_of_date}],
            "cumulative": True,
        }
    if re.search(r"\blo\b.*\bgan het han nhat\b", text):
        if not current_point:
            return None
        metric, group, limit, shape = "lot_expiry", None, None, "date"
        expressions = [{"kind": "today"}]
    elif collection:
        metric, group, limit, shape = "debt_collection", None, None, "number"
    elif re.search(
        r"\b(tong tien nhap hang|tong gia tri cac phieu nhap|tong tien hang nhap vao)\b", text
    ):
        metric, group, limit, shape = "purchase_amount", None, None, "number"
    elif (re.search(r"\b(ton|kho)\b", text) or "cuoi ngay hom nay" in text) and re.search(
        r"\b(hom nay|hnay)\b", text
    ):
        metric, group, limit, shape = "stock_balance", None, None, "number"
        if not current_point:
            return None
        expressions = [{"kind": "today"}]
    else:
        rank = bool(re.search(r"\b(nhat|top)\b", text))
        customer = bool(re.search(r"\bkhach\b", text))
        count = re.search(r"\b(?:top\s*)?([1-9]\d?) (?:mat hang|hang|mon|khach)", text)
        if rank and re.search(r"\b(lai gop|loi nhat)\b", text):
            metric, group = "gross_profit", "product"
        elif rank and re.search(
            r"\b(ban chay|chay nhat|ban nhieu nhat|ban duoc nhieu nhat|hot nhat)\b", text
        ):
            metric, group = "quantity", "product"
        elif (
            (rank or count)
            and customer
            and re.search(r"\b(nhieu tien|tong tien mua|mua nhieu lan)\b", text)
        ):
            metric = "order_count" if re.search(r"\b(so don|mua nhieu lan)\b", text) else "revenue"
            group = "customer"
        elif not rank and re.search(
            (
                "\\b(so|bao nhieu|may|bao nhiu) "
                "(kg|vien|goi|chai|lon|cai)\\b|\\bban.*(?:kg|vien|goi|chai|lon|cai)\\b"
            ),
            text,
        ):
            metric, group = "quantity", None
        elif not rank and re.search(r"\b(doanh thu|dthu|ban.*bao nhieu tien)\b", text):
            # An entity total only; whole-shop S1 remains on its existing path.
            if not re.search(
                r"\b(san pham|mat hang|khach)\b|\bban (?!duoc|dc)[a-z].+? duoc bao nhieu tien", text
            ):
                return None
            metric, group = "revenue", None
        else:
            return None
        limit = int(count[1]) if count else 1 if group else None
        shape = "list" if count else "string" if group else "number"
    if len(expressions) != 1:
        return None
    return {
        "metric": metric,
        "group": group,
        "limit": limit,
        "type": shape,
        "expressions": expressions,
    }


def business_schema(schema: dict, spec: dict, candidates: list[dict]) -> dict:
    from src.agent.plan import TEXT, _array, _object

    props = schema["properties"]
    size = 3 if spec.get("trend") else 1
    for key in ["aggregations", "time_windows"]:
        props[key].update(minItems=size, maxItems=size)
    props["derived_measures"].update(minItems=0, maxItems=0)
    props["time_windows"]["items"] = []
    for i, expression in enumerate(spec["expressions"]):
        props["time_windows"]["items"].append(
            _object(
                {
                    "id": {"const": f"period{i}"},
                    "expression": _object({k: {"const": v} for k, v in expression.items()}),
                }
            )
        )
    aggregate = deepcopy(props["aggregations"]["items"])
    aggregate["properties"].pop("unit", None)
    aggregate["properties"].update(
        metric_id={"const": spec["metric"]},
        scope={"const": "whole_business"},
        filters=_array(TEXT, maximum=0),
        group_by=_array(
            {"const": spec["group"]} if spec["group"] else TEXT,
            minimum=int(bool(spec["group"])),
            maximum=int(bool(spec["group"])),
        ),
    )
    aggregate["properties"].pop("limit", None)
    if spec["limit"]:
        aggregate["properties"]["limit"] = {"const": spec["limit"]}
        aggregate["required"].append("limit")
    props["aggregations"]["items"] = []
    for i in range(size):
        item = deepcopy(aggregate)
        item["properties"].update(id={"const": f"total{i}"}, window_ref={"const": f"period{i}"})
        props["aggregations"]["items"].append(item)
    needs_entity = (
        spec["group"] is None
        and not spec.get("trend")
        and spec["metric"] in {"quantity", "stock_balance", "revenue", "lot_expiry"}
    )
    props["entities"].update(minItems=int(needs_entity), maxItems=int(needs_entity))
    if needs_entity and candidates:
        props["entities"]["items"] = {"enum": [r["entity_key"] for r in candidates]}
    props["output"]["properties"].update(
        expected_type={"const": spec["type"]},
        result_refs=_array(
            {"enum": [f"total{i}" for i in range(size)]}, minimum=size, maximum=size
        ),
    )
    return schema


def _check_business_scope(question: Question, spec: dict, entity: dict | None) -> None:
    text = plain(question.question)
    if entity:
        names = [entity["name"], entity["source_code"] or ""]
        for name in sorted((n for n in names if n), key=len, reverse=True):
            # A grounded entity may follow "của". Other possessive qualifiers
            # remain scope restrictions; they cannot become a whole-shop total.
            text = re.sub(r"(?:\bcua\s+)?" + _alias_pattern(plain(name)), " ", text)
    text = re.sub(r"\bcua (?:cua hang|shop)\b|\bcua hang\b", " ", text)
    cardinalities = re.findall(r"\btop\s*(\d+)\b|\b(\d+) (?:mat hang|hang|mon|khach)\b", text)
    if cardinalities:
        requested = int(cardinalities[0][0] or cardinalities[0][1])
        if (
            len(cardinalities) != 1
            or not 1 <= requested <= 99
            or not spec["group"]
            or spec["limit"] != requested
            or spec["type"] not in {"list", "table"}
        ):
            raise ContractError("question_ranking_cardinality_unsupported")
    if (
        spec["group"] is None
        and spec["metric"]
        in {"receivable", "receivable_customer_count", "debt_collection", "purchase_amount"}
        and re.search(r"\b(top|nhat)\b", text)
    ):
        raise ContractError("ranking_cannot_become_whole_business_balance")
    if (entity or spec["group"] == "product") and re.search(r"\bkhach\b", text):
        raise ContractError("customer_scope_unsupported_for_product_metric")
    if spec["group"] == "customer" and re.search(r"\b(san pham|mat hang)\b", text):
        raise ContractError("product_scope_unsupported_for_customer_ranking")
    if re.search(r"\bcua\b|\b(nha cung cap|doi voi|cho khach)\b", text):
        raise ContractError("business_question_scope_unsupported")
    if spec["metric"] in {"receivable", "receivable_customer_count", "debt_collection"}:
        if re.search(
            r"\bkhach hang (?!(?:con|no|nowj|tra|mua|nao|dang|chua|co|nhieu|cong)\b)"
            r"|\bkhach(?: hang)? (?:co ten|ten|ma)\b",
            text,
        ):
            raise ContractError("named_customer_debt_scope_unsupported")


def validate_business_plan(
    draft: dict, question: Question, context: BusinessContext, spec: dict
) -> dict:
    from src.agent.plan import VERSION
    from src.semantics.metric_registry import metric_contract
    from src.temporal.resolver import resolve_window

    if question.business_id != context.catalog["business_id"] or draft["uncertainties"]:
        raise ContractError("namespace_or_uncertainty")
    size = len(spec["expressions"])
    if (
        len(draft["aggregations"]) != size
        or len(draft["time_windows"]) != size
        or draft["derived_measures"]
    ):
        raise ContractError("business_dependencies_mismatch")
    plan = deepcopy(draft)
    ids = [n["id"] for n in [*draft["time_windows"], *draft["aggregations"]]]
    if len(set(ids)) != len(ids) or any(not x.strip() for x in ids):
        raise ContractError("dependency_ids_invalid")
    windows = {w["id"]: w for w in draft["time_windows"]}
    requirements, refs = [], []
    for aggregate, expression in zip(plan["aggregations"], spec["expressions"], strict=True):
        if (
            aggregate["metric_id"] != spec["metric"]
            or aggregate["scope"] != "whole_business"
            or aggregate["filters"]
            or aggregate["group_by"] != ([spec["group"]] if spec["group"] else [])
            or aggregate.get("limit") != spec["limit"]
        ):
            raise ContractError("business_metric_scope_mismatch")
        window = windows.get(aggregate["window_ref"])
        if not window or window["expression"] != expression:
            raise ContractError("question_time_mismatch_before_coverage")
        resolved = resolve_window(expression, question.as_of_date, context.rules, context.calendar)
        contract = metric_contract(
            context.rules,
            spec["metric"],
            shape=context.prepared.summary.get("shape") if context.prepared else None,
        )
        if "unit" in aggregate and aggregate["unit"] != contract.unit:
            raise ContractError("unit_mismatch")
        refs += contract.rule_refs + list(resolved.rule_refs)
        aggregate.update(
            unit=contract.unit, status_policy=contract.status_policy, time_axis=contract.time_axis
        )
        requirements.append(
            {
                "metric_id": spec["metric"],
                "required_fields": contract.required_fields,
                "window_refs": [window["id"]],
            }
        )
    if set(windows) != {a["window_ref"] for a in plan["aggregations"]}:
        raise ContractError("unused_window")
    if draft["output"] != {
        "expected_type": spec["type"],
        "result_refs": [a["id"] for a in plan["aggregations"]],
    }:
        raise ContractError("business_output_or_order_mismatch")
    needs_entity = (
        spec["group"] is None
        and not spec.get("trend")
        and spec["metric"] in {"quantity", "stock_balance", "revenue", "lot_expiry"}
    )
    if needs_entity:
        candidates = entity_candidates(context, question.question)
        if len(candidates) != 1 or draft["entities"] != [candidates[0]["entity_key"]]:
            raise ContractError("question_entity_unsupported_missing_ambiguous_or_not_grounded")
        entity = candidates[0]
        if spec["metric"] in {"quantity", "stock_balance"}:
            remaining = question.question.casefold()
            for name in [entity["name"], entity["source_code"] or ""]:
                if name:
                    remaining = re.sub(_alias_pattern(name), " ", remaining)
            if not entity["base_unit"] or not re.search(
                _alias_pattern(entity["base_unit"]), remaining
            ):
                raise ContractError("explicit_base_unit_required")
        plan["resolved_entity"] = entity
    elif draft["entities"]:
        raise ContractError("unexpected_entity_filter")
    _check_business_scope(question, spec, plan.get("resolved_entity"))
    if spec["group"]:
        if any(
            entity_candidates(context, question.question, kind) for kind in ("product", "customer")
        ):
            raise ContractError("question_entity_filter_unsupported_for_ranking")
        for rule in [
            "ranking_ties",
            "item_role" if spec["group"] == "product" else "customer_identity",
        ]:
            context.rules.require(rule)
            refs.append(rule)
    plan.update(
        version=VERSION,
        question_id=question.id,
        business_id=question.business_id,
        as_of_date=question.as_of_date,
        rulebook_version=context.rules.sha256,
        rule_refs=list(dict.fromkeys(refs)),
        requirements=requirements,
        business_spec=spec,
    )
    return plan
