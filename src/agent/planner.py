"""Versioned S1 prompt, bounded model repair and deterministic execution fallback."""

from __future__ import annotations

import time

from src.agent.context import build_context
from src.agent.execution import execute_plan
from src.agent.plan import generation_schema, validate_query_plan
from src.data.catalog import BusinessContext
from src.data.split import Question
from src.jsonio import ContractError, dumps, loads

PROMPT_VERSION = "planner-v7"
SYSTEM = """Lập QueryPlan JSON theo schema, không trả lời số hoặc sinh SQL/code.
S1: một kỳ, toàn shop, output number. Doanh thu/tiền bán/dthu=revenue;
số đơn/mấy đơn/số bill=order_count. Số lượng hàng/kg không phải số đơn.
Riêng sản phẩm/khách/nhân viên, trạng thái riêng, phủ định điều kiện, ranking,
so sánh/tỷ lệ/trung bình/nhiều kỳ: uncertainties=["unsupported_scope_or_operator"].
Giữ mọi điều kiện, không biến câu ngoài phạm vi thành tổng toàn shop.
Coverage unknown vẫn lập plan; preflight riêng kiểm tra dữ liệu, không tự suy coverage.
Ví dụ số đơn tháng này:
{"entities":[],"time_windows":[{"id":"period","expression":{"kind":"current_month"}}],
"aggregations":[{"id":"total","metric_id":"order_count","window_ref":"period",
"scope":"whole_business","filters":[],"group_by":[]}],"derived_measures":[],
"output":{"expected_type":"number","result_refs":["total"]},"uncertainties":[]}
Hôm nay=today; hôm qua/hqua=yesterday; tháng này=current_month;
tháng trước=previous_month; tuần này/trước=current_week/previous_week;
current_month chỉ từ ngày 1 đến as_of_date; month là trọn tháng, có thể gồm ngày sau as_of.
Không đổi "tháng này" thành month(year,month), vì sẽ tính cả ngày chưa xảy ra.
N ngày gần nhất=last_n_days(days); ngày cụ thể=day(date ISO);
khoảng ngày=range(start,end ISO); tháng M/Y=month(year,month);
quý Q/Y=quarter(year,quarter); năm Y=year(year); từ đầu năm=year_to_date.
Thiếu năm tháng/quý: luật missing_year theo as_of_date, không theo ngày máy.
Tết=tet(year), trước Tết=before_tet(year). Không đoán khi thời gian mơ hồ.
Server gắn identity, requirements, policy, rule_refs từ runtime authority.
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
    name = "local-s1"
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
        stock = bool(selected.get("snapshot"))
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
                decision = execute_plan(draft, question, context)
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
