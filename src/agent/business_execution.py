"""Fixed one-to-one reductions for the bounded entity/business QueryPlan operators."""

from __future__ import annotations

import re
import sqlite3
import time
from contextlib import closing
from decimal import Decimal, localcontext

from src.contracts import validate_answer
from src.data.catalog import BusinessContext
from src.data.normalization.values import exact_add
from src.data.split import Question
from src.jsonio import ContractError
from src.semantics.rulebook import plain


def execute_business_plan(
    plan: dict, question: Question, context: BusinessContext, deadline: float | None = None
) -> dict:
    from src.agent.execution import _rounded
    from src.semantics.answerability import preflight

    answer = {"id": question.id, "type": "abstain"}
    checks = []
    spec = plan["business_spec"]
    try:
        if deadline is not None and time.monotonic() >= deadline:
            raise ContractError("question_timeout_before_execution")
        windows = {w["id"]: w for w in plan["time_windows"]}
        for aggregate in plan["aggregations"]:
            check = preflight(
                context,
                aggregate["metric_id"],
                windows[aggregate["window_ref"]]["expression"],
                as_of_date=question.as_of_date,
            )
            check["aggregation_ref"] = aggregate["id"]
            checks.append(check)
        plan["preflight"] = checks
        stop = next((c for c in checks if c["decision"] == "abstain"), None) or next(
            (c for c in checks if c["decision"] == "unanswerable"), None
        )
        if stop:
            if stop["decision"] == "unanswerable":
                answer = {"id": question.id, "type": "unanswerable", "reason": stop["reason"]}
            return {
                "answer": answer,
                "plan": plan,
                "execution": {"status": "not_performed", "reason": stop["reason"]},
            }
        results = [_reduce(context, plan, c, deadline) for c in checks]
        value = results[0]["value"]
        if spec.get("trend"):
            values = [r["value"] for r in results]
            value = all(b > a for a, b in zip(values, values[1:], strict=False))
        elif spec["type"] == "number" and spec["metric"] not in {
            "quantity",
            "stock_balance",
            "receivable_customer_count",
        }:
            value = _rounded(
                value, context.semantics["metrics"][spec["metric"]]["contract"]["rounding"]
            )
        if deadline is not None and time.monotonic() >= deadline:
            raise ContractError("question_timeout_after_execution")
        answer = {"id": question.id, "type": spec["type"], "answer": value}
        validate_answer(answer)
        return {
            "answer": answer,
            "plan": plan,
            "execution": {
                "status": "verified",
                "aggregations": results,
                "checks": [
                    "one_to_one_keys",
                    "all_required_rows",
                    "exact_decimal",
                    "question_entity_scope",
                    "source_code_ties",
                    "typed_output",
                ],
            },
        }
    except (ContractError, sqlite3.Error, ArithmeticError, ValueError, OSError) as exc:
        return {
            "answer": answer,
            "plan": plan,
            "execution": {"status": "failed", "reason": str(exc)},
        }


def _reduce(context: BusinessContext, plan: dict, check: dict, deadline: float | None) -> dict:
    spec, metric = plan["business_spec"], check["metric_id"]
    if spec.get("trend"):
        from src.agent.execution import _compute

        result = _compute(context, check, round_result=False, deadline=deadline)
        return {
            "metric_id": metric,
            "value": result["exact_value"],
            "effective_window": check["effective_window"],
        }
    issues = context.prepared.summary.get("extensions", {}).get("issues", {})
    if not context.prepared.summary.get("extensions", {}).get("counts"):
        raise ContractError("business_facts_unsupported")
    start, end = check["effective_window"]["start"], check["effective_window"]["end"]
    started = time.monotonic()

    def budget():
        if time.monotonic() - started > 15 or (
            deadline is not None and time.monotonic() >= deadline
        ):
            raise ContractError("execution_timeout")

    group = spec["group"]
    selected = plan.get("resolved_entity", {}).get("entity_key")
    totals, total, matched = {}, Decimal(0), 0
    with closing(context.prepared.connect()) as conn:
        conn.set_progress_handler(
            lambda: int(
                time.monotonic() - started > 15
                or (deadline is not None and time.monotonic() >= deadline)
            ),
            1000,
        )
        entities = {r["entity_key"]: r for r in context.prepared.iter_records("entities")}
        coded_customers = "customer_id" in context.prepared.summary.get("mapping", {}).get(
            "fields", {}
        ) or any(e["kind"] == "customer" and e["source_code"] for e in entities.values())

        def customer_identity(key: str | None) -> str | None:
            # A name recorded on an anonymous order cannot become a customer in
            # a shop that identifies customers by code. It still contributes to
            # whole-shop revenue/debt under those metrics' own policies.
            return None if key and coded_customers and not entities[key]["source_code"] else key

        def add(key, amount):
            nonlocal total, matched
            if not amount.is_finite():
                raise ContractError("nonfinite_business_value")
            total = exact_add(total, amount)
            matched += 1
            if key:
                totals[key] = exact_add(totals.get(key, Decimal(0)), amount)

        if metric in {"receivable", "receivable_customer_count", "debt_collection"}:
            if not re.search(
                r"cong no.*moi lan khach tra tien.*so khach phai tra cho ca don",
                plain(context.catalog["readme"]),
            ):
                raise ContractError("payment_history_and_order_payable_authority_missing")
            if any(k in issues for k in ["debt", "payments"]):
                raise ContractError("debt_payment_mapping_unsupported_or_conflict")
            paid, collections = {}, {}
            for order, at, raw in conn.execute("SELECT order_key,paid_at,amount FROM payments"):
                budget()
                day = at[:10]
                if day <= end:
                    paid[order] = exact_add(paid.get(order, Decimal(0)), Decimal(raw))
                collections.setdefault(order, []).append((day, Decimal(raw)))
            for order, created, status, customer, payable, method, channel in conn.execute(
                "SELECT "
                "o.order_key,o.ordered_at,o.status,p.customer_key,p.payable,p.payment_method,"
                "s.channel FROM orders o LEFT JOIN order_parties p USING(order_key) "
                "LEFT JOIN order_scopes s USING(order_key)"
            ):
                budget()
                if created[:10] > end or status == "cancelled":
                    continue
                if not method or plain(method) not in {
                    "cash",
                    "transfer",
                    "ewallet",
                    "cod",
                    "debt",
                    "marketplace",
                    "tmdt",
                }:
                    raise ContractError("order_payment_method_missing_or_unknown")
                if plain(method) in {"cod", "marketplace", "tmdt"}:
                    continue
                if channel is not None:
                    channel_label = plain(channel)
                    if channel_label in {
                        "marketplace",
                        "tmdt",
                        "san tmdt",
                        "shopee",
                        "lazada",
                        "tiki",
                        "tiktok_shop",
                    }:
                        continue
                    if channel_label not in {"store", "phone", "zalo_oa", "counter"}:
                        raise ContractError("debt_sales_channel_policy_unsupported")
                customer = customer_identity(customer)
                if metric == "debt_collection":
                    if plain(method) == "debt":
                        for day, amount in collections.get(order, []):
                            if start <= day <= end and day > created[:10]:
                                add(customer, amount)
                else:
                    if payable is None:
                        raise ContractError("payable_missing")
                    balance = exact_add(Decimal(payable), paid.get(order, Decimal(0)).copy_negate())
                    if balance > 0:
                        add(customer, balance)
            if metric == "receivable_customer_count":
                total = Decimal(len(totals))
        elif metric == "purchase_amount":
            if "purchases" in issues:
                raise ContractError("purchase_mapping_unsupported")
            for day, status, amount in conn.execute(
                "SELECT purchased_at,status,amount FROM purchases"
            ):
                budget()
                if start <= day[:10] <= end and plain(status) in {"received", "da nhap"}:
                    add(None, Decimal(amount))
        elif metric == "lot_expiry":
            if "lots" in issues:
                raise ContractError("lot_keys_allocations_or_authority_unsupported")
            confirmed_end = context.semantics["coverage"]["confirmed_end"]
            if start != end or end != confirmed_end:
                raise ContractError("lot_snapshot_only_at_declared_export_end")
            if "mot dong ban co the lay tu nhieu lo. hang khach tra ve lai lo da ban" not in plain(
                context.catalog["readme"]
            ):
                raise ContractError("lot_allocation_return_authority_unsupported")
            balances = {}
            for lot, expiry, quantity, purchased, status in conn.execute(
                (
                    "SELECT l.lot_id,l.expiry,l.received_quantity,p.purchased_at,p.status"
                    " FROM lots l JOIN purchases p ON l.purchase_id=p.purchase_key WHERE "
                    "l.product_key=?"
                ),
                (selected,),
            ):
                budget()
                if purchased[:10] <= end and plain(status) in {"received", "da nhap"}:
                    balances[lot] = [expiry, Decimal(quantity)]
            for lot, sold, returned, returned_at, created, status in conn.execute(
                "SELECT "
                "a.lot_id,a.quantity,a.returned_quantity,a.returned_at,o.ordered_at,o.status"
                " FROM lot_allocations a JOIN orders o USING(order_key)"
            ):
                budget()
                if lot in balances and created[:10] <= end and status != "cancelled":
                    balances[lot][1] = exact_add(balances[lot][1], Decimal(sold).copy_negate())
                    if returned_at and returned_at[:10] <= end:
                        balances[lot][1] = exact_add(balances[lot][1], Decimal(returned))
            if any(amount < 0 for _, amount in balances.values()):
                raise ContractError("negative_lot_balance_conflict")
            expiries = [
                expiry for expiry, amount in balances.values() if amount > 0 and expiry >= end
            ]
            if not expiries:
                raise ContractError("no_nonexpired_remaining_lot")
            return {
                "metric_id": metric,
                "value": min(expiries),
                "matched_records": len(expiries),
                "effective_window": check["effective_window"],
            }
        elif metric == "stock_balance":
            if "stock" in issues:
                raise ContractError("stock_mapping_unsupported")
            extension = context.prepared.summary["extensions"]
            if extension.get("stock_mode") == "snapshot":
                if start != end or end != extension["snapshot_at"]:
                    raise ContractError("snapshot_date_unsupported")
            elif "ton o dong cuoi cua san pham tinh den thoi diem do" not in plain(
                context.catalog["readme"]
            ):
                raise ContractError("stock_balance_authority_unsupported")
            rows = conn.execute(
                (
                    "SELECT occurred_at,balance FROM stock_balances WHERE product_key=? AND "
                    "substr(occurred_at,1,10)<=? ORDER BY occurred_at DESC"
                ),
                (selected, end),
            )
            latest = rows.fetchone()
            if not latest:
                raise ContractError("no_prior_stock_balance")
            for at, amount in rows:
                budget()
                if at != latest[0]:
                    break
                if Decimal(amount) != Decimal(latest[1]):
                    raise ContractError("simultaneous_stock_balances_ambiguous")
            add(selected, Decimal(latest[1]))
        else:
            if context.prepared.summary.get("shape") in {"order_headers", "sales_ledger"}:
                raise ContractError("entity_sales_grain_unsupported")
            family = "customer" if group == "customer" else "product"
            if family in issues:
                raise ContractError("entity_mapping_unsupported_or_conflict")
            if metric == "gross_profit" and not re.search(
                r"gia von.*la gia von mot don vi ban", plain(context.catalog["readme"])
            ):
                raise ContractError("line_cost_selling_unit_authority_missing")
            seen_orders = set()
            sql = (
                "SELECT o.order_key,o.ordered_at,o.status,l.line_key,l.net_amount,l.row_role,"
                "i.product_key,i.base_quantity,i.sold_quantity,i.unit_cost,p.customer_key"
                " FROM orders o JOIN order_lines l USING(order_key) LEFT JOIN line_items "
                "i USING(line_key) LEFT JOIN order_parties p USING(order_key)"
            )
            for (
                order,
                created,
                status,
                _line,
                amount,
                role,
                product,
                quantity,
                sold,
                cost,
                customer,
            ) in conn.execute(sql):
                budget()
                if (
                    not start <= created[:10] <= end
                    or status not in check["contract"]["status_policy"]
                ):
                    continue
                if role not in {"sale_item", "service_fee", "return_item", "order_adjustment"}:
                    raise ContractError("row_role_unknown")
                if metric in {"quantity", "gross_profit"} or group == "product":
                    if role != "sale_item":
                        continue
                    if product is None:
                        raise ContractError("product_identity_missing")
                if selected and product != selected:
                    continue
                customer = customer_identity(customer)
                if group == "customer" and customer is None:
                    continue
                if metric == "quantity":
                    if quantity is None:
                        raise ContractError("base_quantity_missing")
                    value = Decimal(quantity)
                elif metric == "order_count":
                    if order in seen_orders:
                        continue
                    seen_orders.add(order)
                    value = Decimal(1)
                else:
                    if role == "order_adjustment":
                        continue
                    if amount is None:
                        raise ContractError("line_amount_missing")
                    value = Decimal(amount)
                    if metric == "gross_profit":
                        if cost is None or sold is None:
                            raise ContractError("line_cost_or_selling_quantity_missing")
                        with localcontext() as arithmetic:
                            arithmetic.prec = (
                                len(Decimal(sold).as_tuple().digits)
                                + len(Decimal(cost).as_tuple().digits)
                                + 4
                            )
                            line_cost = Decimal(sold) * Decimal(cost)
                        value = exact_add(value, line_cost.copy_negate())
                add(customer if group == "customer" else product, value)
        if group:
            ranked = sorted(
                totals,
                key=lambda key: (
                    totals[key].copy_negate(),
                    entities[key]["source_code"] or entities[key]["name"],
                ),
            )[: spec["limit"]]
            if spec["type"] == "string":
                if not ranked:
                    raise ContractError("empty_entity_ranking")
                value = entities[ranked[0]]["name"]
            elif spec["type"] == "table":
                from src.agent.execution import _rounded

                value = {
                    "columns": ["khach_hang", "so_tien_no"],
                    "rows": [
                        [
                            entities[k]["name"],
                            _rounded(
                                totals[k],
                                context.semantics["metrics"][metric]["contract"]["rounding"],
                            ),
                        ]
                        for k in ranked
                    ],
                }
            else:
                value = [entities[k]["name"] for k in ranked]
        else:
            value = total
    return {
        "metric_id": metric,
        "value": value,
        "matched_records": matched,
        "effective_window": check["effective_window"],
    }
