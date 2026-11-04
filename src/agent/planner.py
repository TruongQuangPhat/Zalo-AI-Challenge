"""Versioned bounded planner and deterministic execution fallback."""

from __future__ import annotations

import re
import time

from src.agent.context import build_context
from src.agent.execution import execute_plan
from src.agent.plan import generation_schema, validate_query_plan
from src.data.catalog import BusinessContext
from src.data.split import Question
from src.jsonio import ContractError, dumps, loads

PROMPT_VERSION = "planner-v13"
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


class LocalPlanner:
    name = "local-planner"
    is_mock = False

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
        selected = build_context(question, context)
        from src.semantics.rulebook import plain

        stock = bool(selected.get("snapshot")) and bool(
            re.search(r"\b(ton|kho)\b", plain(question.question))
        )
        schema = generation_schema(
            question,
            stock=stock,
            entity_codes=[e["product_code"] for e in selected["inventory_entities"]],
        )
        messages = [
            {"role": "system", "content": STOCK_SYSTEM if stock else SYSTEM},
            {"role": "user", "content": dumps(selected)},
        ]
        attempts = []
        deadline = time.monotonic() + self.timeout
        for _ in range(self.attempts):
            record = {}
            try:
                record = self.model.generate(messages, schema, deadline - time.monotonic())
                attempts.append(record)
                if record.get("finish_reason") != "stop":
                    raise ContractError("incomplete_generation")
                draft = loads(record["text"])
                validate_query_plan(draft, question, context)
                decision = execute_plan(draft, question, context, deadline=deadline)
                decision["plan"]["planning"] = {
                    "prompt_version": PROMPT_VERSION,
                    "attempts": attempts,
                    "context_version": selected["version"],
                }
                return decision
            except (ContractError, OSError, ValueError) as exc:
                if not record:
                    attempts.append(record)
                record["error"] = str(exc)
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
                "prompt_version": PROMPT_VERSION,
                "attempts": attempts,
            },
            "execution": {"status": "not_performed", "reason": "planning_failed"},
        }
