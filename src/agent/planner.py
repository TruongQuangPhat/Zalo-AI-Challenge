"""Versioned bounded planner and deterministic execution fallback."""

from __future__ import annotations

import re
import time

from src.agent.context import build_context
from src.agent.execution import execute_plan
from src.agent.plan import generation_schema, unparsed_time
from src.data.catalog import BusinessContext
from src.data.split import Question
from src.jsonio import ContractError, dumps, loads

PROMPT_VERSION = "planner-v15"
SYSTEM = """Lập QueryPlan JSON theo schema; không trả số đáp án, SQL hoặc code.
Giữ mọi điều kiện. Ngoài scope/schema: uncertainties=["unsupported_scope_or_operator"].
Trong scope thì uncertainties=[]; không ghi uncertainty chỉ vì không có raw rows.
revenue=doanh thu/dthu/tiền bán; order_count=số đơn không huỷ/trả;
cancelled_order_count=số đơn huỷ; placed_order_count=mọi trạng thái.
Một tổng: một window/aggregate, derived_measures=[], output number.
Ví dụ số đơn tháng này (template, không phải đáp án):
{"entities":[],"time_windows":[{"id":"period","expression":{"kind":"current_month"}}],
"aggregations":[{"id":"total","metric_id":"order_count","window_ref":"period",
"scope":"whole_business","filters":[],"group_by":[]}],"derived_measures":[],
"output":{"expected_type":"number","result_refs":["total"]},"uncertainties":[]}
S2 difference=current-baseline; percent_change=(current-baseline)/baseline*100,
output percent. Dùng đúng refs/kỳ hỏi/kỳ so; không tự tính. Baseline 0/âm: executor abstain.
Tháng M/Y so tháng trước: baseline M-1 của kỳ hỏi, không theo as_of_date.
Tháng này so tháng trước: current_month/previous_mtd, cùng số ngày.
AOV: average_order_value, revenue/order_count, number.
Tỉ lệ huỷ: cancellation_rate, cancelled_order_count/placed_order_count, percent.
Trung bình ngày/thứ: average_daily, metric/calendar_day_count, number;
weekday=0 thứ Hai,...,6 Chủ nhật trên cả hai aggregates; đếm cả ngày không bán.
Doanh thu nhóm: một revenue có item_group/customer_group theo câu hỏi.
Tỉ trọng: share, revenue/revenue, percent; chỉ tử có item_group=<nhóm>,
customer_group=<nhóm khách> không dấu hoặc channel=phone; mẫu toàn shop filters=[].
Ca sáng shift=morning; ca chiều shift=afternoon. So ca dùng một window, hai
aggregates cùng metric, filters riêng. Ca chiều so ca sáng: baseline là sáng.
Nhiều số độc lập: output list, result_refs đúng thứ tự hỏi, derived_measures=[].
Chuỗi 3 tháng kết thúc M/Y: 3 windows/aggregates theo thứ tự tháng cũ tới mới.
Hai kênh: channel=phone/counter theo thứ tự hỏi; một window chung.
Không sort kết quả theo giá trị. scope=whole_business, group_by=[], entities=[].
Hôm nay=today; hôm qua/hqua=yesterday; tuần trc/trước=previous_week;
tuần này=current_week; tháng này=current_month; tháng trước=previous_month;
N ngày gần nhất=last_n_days; từ đầu năm=year_to_date; tháng M/Y=month;
quý Q/Y=quarter; năm Y=year; ngày cụ thể=day; khoảng ngày=range;
Tết=tet, trước Tết=before_tet. Dùng as_of_date, không dùng ngày máy.
Current_month chỉ đến as_of; month là cả tháng, có thể có ngày sau as_of.
Server gắn identity, policy, requirements, resolve/preflight. Coverage unknown vẫn
lập plan; missing order_identity vẫn lập count để preflight xác minh unanswerable.
Không thay count bằng số dòng. Không bỏ điều kiện sản phẩm/khách/ngưỡng/ranking.
"""

STOCK_SYSTEM = """Lập QueryPlan JSON để tra tồn kho, không trả lời số và không sinh SQL/code.
Chỉ hỗ trợ inventory_on_hand: một sản phẩm, cuối ngày hôm nay đúng mốc snapshot README,
đơn vị cơ bản đúng nguồn, output number. Không tái dựng quá khứ, đổi đơn vị, cộng nhiều
sản phẩm hay tính giá trị tồn. Quantity chỉ executor được đọc; context chỉ có identities.
Chọn một product_code hoặc product_name trong inventory_entities, chép nguyên vào entities.
Chép GIÁ TRỊ product_code, không chép tên key hay placeholder. Schema chỉ cho mã nguồn.
unit phải là base_unit nguồn và đúng đơn vị được hỏi. Không chọn gần đúng khi tên mơ hồ.
Nếu không đủ scope/entity/unit/time, ghi uncertainties, không bỏ điều kiện của câu hỏi.
Template (placeholder, không phải dữ liệu):
{"entities":["source_product_code"],"time_windows":[{"id":"point","expression":{"kind":"today"}}],
"aggregations":[{"id":"stock","metric_id":"inventory_on_hand","window_ref":"point",
"scope":"product","filters":[],"group_by":[],"unit":"source_base_unit"}],
"derived_measures":[],"output":{"expected_type":"number","result_refs":["stock"]},
"uncertainties":[]}
History available_span unknown không chứng minh snapshot sai; preflight kiểm tra riêng
mốc snapshot, identity và đơn vị, không mở coverage cho revenue/order_count.
"""


BUSINESS_SYSTEM = """Lập QueryPlan JSON theo schema. Không trả numeric answer, SQL, code.
operation là phạm vi đã nhận diện từ câu hỏi; giữ metric/group/limit/time và entity.
Mỗi time window có một aggregation. entities chỉ chứa entity_key của ứng viên được
nhắc trong câu, khi cần một sản phẩm. Không chọn gần đúng hoặc gộp mã khác nhau.
Xếp hạng: group_by=[product] hoặc [customer], limit=top k, output string nếu hỏi
một đối tượng; list khi top k; table khi hỏi khách kèm số nợ. Không tính tên/đáp án.
Số tổng/quantity/kho/nợ/thu nợ/nhập: output number. Mọi filters=[];
derived_measures=[]. Boolean trend dùng 3 windows và 3 revenue aggregates theo
thứ tự tháng, output boolean và result_refs đúng thứ tự. Executor tính so sánh.
Trong scope thì uncertainties=[]; ngoài scope hoặc entity mơ hồ thì ghi uncertainty.
Thiếu source/coverage không tự đoán dữ liệu; server preflight quyết định.
"""


# Intents whose rulebook definition has no executor operator yet. Without one, a plan
# silently falls back to a whole-business metric, so abstain before planning.
UNSUPPORTED_INTENTS = {
    "unit_price": r"\b(don gia|gia ban)\b|\bgia (mot|1) ",
    "size_quantity": r"\bsize (m|l)\b",
    "delivery_batches": r"\bdot (giao|hang)\b|\b(may|bao nhieu|bao nhiu|bn|bnhiu|nhieu|so) dot\b",
}


def unsupported_intent(question: Question) -> str | None:
    from src.semantics.rulebook import plain

    text = plain(question.question)
    intent = next((k for k, p in UNSUPPORTED_INTENTS.items() if re.search(p, text)), None)
    # A date-like token no finite cue understands leaves the period to the model, which
    # then copies the prompt template's period instead of the one asked.
    return intent or ("unparsed_time" if unparsed_time(question) else None)


_ASK = r"(?:bao nhieu|bao nhiu|bnhiu|bnhieu|bn|nhieu)"
# Question wording that fixes the answer shape regardless of the plan.
_LIST_CUES = (
    r"\b(lan luot|moi loai|moi ben|tung thang|moi thang)\b",
    _ASK + r"\b.*\b" + _ASK + r"\b",
    r"\bva (?:doanh thu |dthu |so don )?(?:cac )?don\b",
    r"\b(?:top )?(?:[2-9]|10|hai|ba|bon|nam) (?:mat hang|mon|san pham|khach|nhan vien)\b",
    r"\bnhung (?:mat hang|mon|san pham|khach)\b",
)
_DATE_CUE = r"\b(?:ngay|thang) nao\b"
_WHO_CUE = (
    r"\b(?:ai la|la ai|nguoi nao|nhan vien nao|khach nao|khach hang nao|mat hang nao|"
    r"mon nao|san pham nao)\b"
)
_ADVICE_CUE = r"\b(?:duoc khong|co nen|nen dung|co sao khong)\s*[?!.]*$"


def shape_mismatch(question: Question, answer_type: str) -> str | None:
    """Shape the question asks for when the answer cannot have it, else None."""
    from src.semantics.rulebook import plain

    text = plain(question.question)
    if answer_type in {"abstain", "unanswerable"}:
        return None
    if any(re.search(p, text) for p in _LIST_CUES):
        return None if answer_type in {"list", "table"} else "list"
    if re.search(_DATE_CUE, text) and answer_type in {"number", "percent"}:
        return "date"
    if re.search(_WHO_CUE, text) and answer_type in {"number", "percent", "date", "boolean"}:
        return "string"
    if (
        re.search(_ADVICE_CUE, text)
        and not re.search(r"\b" + _ASK + r"\b|\bmay\b", text)
        and answer_type != "boolean"
    ):
        return "advice"
    return None


def _suppress(decision: dict, question: Question, reason: str, **details) -> dict:
    plan = decision["plan"]
    plan["guard"] = {"reason": reason, **details, "suppressed": decision["answer"]}
    return {
        "answer": {"id": question.id, "type": "abstain"},
        "plan": plan,
        "execution": {"status": "suppressed", "reason": reason},
    }


def _guard_answer(decision: dict, question: Question, context: BusinessContext) -> dict:
    if expected := shape_mismatch(question, decision["answer"]["type"]):
        return _suppress(decision, question, "guard:answer_shape", expected=expected)
    return _guard_unbound_entity(decision, question, context)


def _guard_unbound_entity(decision: dict, question: Question, context: BusinessContext) -> dict:
    """Abstain when the question names a source entity but the plan binds none.

    Such plans silently answer with a whole-business metric (e.g. shop revenue for a
    product question), which costs a point; abstaining costs nothing.
    """
    from src.agent.entities import mentioned_entity

    plan = decision["plan"]
    bound = (
        plan.get("resolved_entity")
        or plan.get("entities")
        or any(a.get("filters") or a.get("group_by") for a in plan.get("aggregations", []))
    )
    if decision["answer"]["type"] == "abstain" or bound:
        return decision
    if not (name := mentioned_entity(context, question)):
        return decision
    return _suppress(decision, question, "guard:unbound_entity", mentioned=name)


class LocalPlanner:
    name = "local-planner"

    def __init__(self, model, *, attempts: int = 2, timeout: float = 120):
        if not 1 <= attempts <= 2 or timeout <= 0:
            raise ContractError("planner requires 1..2 attempts and a positive question timeout")
        self.model, self.attempts, self.timeout = model, attempts, timeout

    @property
    def metadata(self) -> dict:
        return {
            "prompt_version": PROMPT_VERSION,
            "attempts": self.attempts,
            "question_timeout_seconds": self.timeout,
            "model": self.model.metadata,
        }

    def decide(self, question: Question, *, context: BusinessContext) -> dict:
        if intent := unsupported_intent(question):
            reason = f"guard:unsupported_intent:{intent}"
            return {
                "answer": {"id": question.id, "type": "abstain"},
                "plan": {"status": "skipped", "reason": reason, "prompt_version": PROMPT_VERSION},
                "execution": {"status": "not_performed", "reason": reason},
            }
        started = time.monotonic()
        deadline = started + self.timeout
        selected = build_context(question, context)
        from src.semantics.rulebook import plain

        stock = bool(selected.get("snapshot")) and bool(
            re.search(r"\b(ton|kho)\b", plain(question.question))
        )
        schema = generation_schema(
            question,
            stock=stock,
            entity_codes=[e["product_code"] for e in selected.get("inventory_entities", [])],
            candidates=selected.get("entity_candidates", []),
        )
        messages = [
            {
                "role": "system",
                "content": STOCK_SYSTEM
                if stock
                else BUSINESS_SYSTEM
                if "operation" in selected
                else SYSTEM,
            },
            {"role": "user", "content": dumps(selected)},
        ]
        attempts = []
        context_seconds = time.monotonic() - started
        for _ in range(self.attempts):
            record = {}
            stage = "generation"
            phase_start = time.monotonic()
            try:
                record = self.model.generate(messages, schema, deadline - time.monotonic())
                record["generation_seconds"] = time.monotonic() - phase_start
                attempts.append(record)
                if record.get("finish_reason") != "stop":
                    raise ContractError("incomplete_generation")
                stage = "json_parse"
                draft = loads(record["text"])
                stage = "semantic_validation"
                decision = execute_plan(draft, question, context, deadline=deadline)
                decision["plan"]["planning"] = {
                    "prompt_version": PROMPT_VERSION,
                    "attempts": attempts,
                    "context_version": selected["version"],
                    "context_seconds": context_seconds,
                }
                return _guard_answer(decision, question, context)
            except (ContractError, OSError, ValueError) as exc:
                if not record:
                    attempts.append(record)
                record.setdefault("generation_seconds", time.monotonic() - phase_start)
                record["error"] = str(exc)
                record["error_stage"] = stage
                record["diagnostics"] = getattr(exc, "diagnostics", {})
                if stage == "semantic_validation" and isinstance(draft, dict):
                    aggregations = draft.get("aggregations")
                    record["intended_metrics"] = (
                        [a.get("metric_id") for a in aggregations if isinstance(a, dict)]
                        if isinstance(aggregations, list)
                        else []
                    )
                # A valid but unsupported/uncertain intent must not be repaired into an
                # executable total by dropping its conditions. Retry syntax only.
                if record.get("text"):
                    try:
                        loads(record["text"])
                    except ContractError:
                        pass
                    else:
                        break
                if time.monotonic() >= deadline:
                    break
                messages.append(
                    {"role": "user", "content": "Invalid JSON; repair once. " + str(exc)}
                )
        return {
            "answer": {"id": question.id, "type": "abstain"},
            "plan": {
                "status": "failed",
                "reason": "planning_failed",
                "context_seconds": context_seconds,
                "prompt_version": PROMPT_VERSION,
                "attempts": attempts,
            },
            "execution": {"status": "not_performed", "reason": "planning_failed"},
        }
