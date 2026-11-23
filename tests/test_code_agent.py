import json
import pickle

import pandas as pd

from src.agent.code_agent import CodeAgent, FallbackPlanner, extract_code, run_code, vote_key
from src.data.split import Question


def _split(tmp_path, readme="# shop\n"):
    shop = tmp_path / "data" / "s1"
    shop.mkdir(parents=True)
    (shop / "README.md").write_text(readme)
    (shop / "ban.csv").write_text("Ngày,Món,Tiền\n2026-01-02,Cà phê,20000\n2026-01-03,Trà,15000\n")
    (tmp_path / "rulebook.md").write_text("# Rulebook\n")
    (tmp_path / "calendar.csv").write_text("date,lunar\n2026-01-01,x\n")
    return tmp_path


class FakeModel:
    metadata = {"sha256": "fake"}

    def __init__(self, texts):
        self.texts = texts

    def sample(self, messages, timeout, **kwargs):
        return {"texts": self.texts, "usage": {}, "seconds": 0}


def test_run_code_reads_table_and_returns_plain_json(tmp_path):
    path = tmp_path / "t.pkl"
    frame = pd.DataFrame([["Ngày", "Tiền"], ["2026-01-02", "20000"], ["2026-01-03", "15000"]])
    pickle.dump({"ban.csv": frame}, path.open("wb"))
    code = (
        "df = table('ban.csv')\nANSWER = {'type': 'number', 'answer': df['Tiền'].astype(int).sum()}"
    )
    assert run_code(code, path, "2026-01-05") == {"answer": {"type": "number", "answer": 35000}}
    assert "error" in run_code("raise SystemExit(3)", path, "2026-01-05")


def test_extract_code_and_vote_key():
    assert extract_code("x\n```python\nANSWER = 1\n```") == "ANSWER = 1\n"
    assert extract_code("không có code") is None
    assert vote_key({"type": "number", "answer": 3}) == vote_key({"type": "number", "answer": 3.0})
    assert vote_key({"type": "number", "answer": 10**400})
    assert vote_key({"type": "string", "answer": " Trà "}) == vote_key(
        {"type": "string", "answer": "trà"}
    )


def test_table_helpers_clean_numbers_dates_and_join_sheets(tmp_path):
    path = tmp_path / "t.pkl"
    a = pd.DataFrame(
        [
            ["Ngày", "Tiền", "Mã"],
            ["1 thg 6 2025", "1.250.000", "0901234567"],
            ["2/6/2025", "560,000", "0907654321"],
        ]
    )
    b = pd.DataFrame([["Ngày", "Tiền", "Mã"], ["3/7/2025", "12k", "0900000000"]])
    pickle.dump({"so.xlsx::T6": a, "so.xlsx::T7": b}, path.open("wb"))
    code = (
        "df = table('so.xlsx')\n"
        "ANSWER = {'type': 'list', 'answer': [df['Tiền'].sum(), str(df['Ngày'].min().date()), "
        "df['Mã'].iloc[0], len(df), sorted(df['sheet'].unique())]}"
    )
    out = run_code(code, path, "2026-01-05")
    assert out == {
        "answer": {"type": "list", "answer": [1822000, "2025-06-01", "0901234567", 3, ["T6", "T7"]]}
    }


def test_month_first_date_columns_are_read_month_first(tmp_path):
    path = tmp_path / "t.pkl"
    frame = pd.DataFrame([["Ngày đặt"], ["06/13/2025"], ["07/02/2025"]])
    pickle.dump({"don.csv": frame}, path.open("wb"))
    code = (
        "ANSWER = {'type': 'list', 'answer': [str(d.date()) for d in table('don.csv')['Ngày đặt']]}"
    )
    out = run_code(code, path, "2026-01-05")
    assert out == {"answer": {"type": "list", "answer": ["2025-06-13", "2025-07-02"]}}


def test_numeric_strings_are_coerced_and_failures_repaired(tmp_path):
    root = _split(tmp_path)
    as_text = "```python\nANSWER = {'type': 'number', 'answer': '1.250.000'}\n```"
    broken = "```python\nANSWER = {'type': 'number', 'answer': undefined_name}\n```"

    class Repairing(FakeModel):
        def sample(self, messages, timeout, **kwargs):
            if messages[-1]["role"] == "user" and "bị lỗi" in messages[-1]["content"]:
                return {"texts": [as_text, as_text], "usage": {}, "seconds": 0}
            return {"texts": self.texts, "usage": {}, "seconds": 0}

    agent = CodeAgent(
        Repairing([as_text, broken, broken, broken]), root, tmp_path / "c", min_votes=3
    )
    out = agent.answer(Question("q1", "s1", "?", "2026-01-05"))
    assert out["answer"] == {"id": "q1", "type": "number", "answer": 1250000}
    assert sum(1 for r in out["log"]["runs"] if r.get("repair")) == 2


def test_agent_answers_only_when_enough_samples_agree(tmp_path):
    root = _split(tmp_path)
    good = "```python\nANSWER = {'type': 'number', 'answer': len(table('ban.csv'))}\n```"
    other = "```python\nANSWER = {'type': 'number', 'answer': 7}\n```"
    question = Question("q1", "s1", "Có mấy dòng?", "2026-01-05")
    agreed = CodeAgent(FakeModel([good, good, good, other]), root, tmp_path / "c", min_votes=3)
    assert agreed.answer(question)["answer"] == {"id": "q1", "type": "number", "answer": 2}
    split = CodeAgent(FakeModel([good, good, other, other]), root, tmp_path / "c", min_votes=3)
    assert split.answer(question)["answer"]["type"] == "abstain"


def test_agent_abstains_on_an_agreed_zero(tmp_path):
    root = _split(tmp_path)
    zero = "```python\nANSWER = {'type': 'number', 'answer': 0}\n```"
    agent = CodeAgent(FakeModel([zero] * 4), root, tmp_path / "c", min_votes=3)
    assert agent.answer(Question("q1", "s1", "?", "2026-01-05"))["answer"]["type"] == "abstain"


def test_agent_keeps_unanswerable_as_abstain_by_default(tmp_path):
    root = _split(tmp_path)
    code = "```python\nANSWER = {'type': 'unanswerable'}\n```"
    agent = CodeAgent(FakeModel([code] * 4), root, tmp_path / "c", min_votes=3)
    out = agent.answer(Question("q1", "s1", "?", "2026-01-05"))
    assert out["answer"]["type"] == "abstain" and out["log"]["top_votes"] == 4


def test_fallback_planner_keeps_rule_answers_and_falls_back_on_abstain(tmp_path):
    root = _split(tmp_path)
    code = "```python\nANSWER = {'type': 'number', 'answer': 5}\n```"
    agent = CodeAgent(FakeModel([code] * 4), root, tmp_path / "c", min_votes=3)

    class Base:
        name, metadata = "base", {}

        def __init__(self, answer):
            self.answer = answer

        def decide(self, question, *, context):
            return {"answer": {"id": question.id, **self.answer}, "plan": {"op": "x"}}

    question = Question("q1", "s1", "?", "2026-01-05")
    ruled = FallbackPlanner(Base({"type": "number", "answer": 1}), agent)
    assert ruled.decide(question, context=object())["answer"]["answer"] == 1
    fell = FallbackPlanner(Base({"type": "abstain"}), agent).decide(question, context=object())
    assert fell["answer"]["answer"] == 5 and fell["execution"] == "code_fallback"
    assert json.dumps(fell["plan"]["fallback"]["votes"])
    missing = FallbackPlanner(Base({"type": "number", "answer": 1}), agent)
    assert missing.decide(question, context=None)["answer"]["answer"] == 5


def test_columns_match_without_accents_and_errors_list_columns(tmp_path):
    path = tmp_path / "t.pkl"
    frame = pd.DataFrame([["Thành tiền", "Trạng thái"], ["20000", "Xong"], ["15000", "Hủy"]])
    pickle.dump({"ban.csv": frame}, path.open("wb"))
    ok = "ANSWER = {'type': 'number', 'answer': table('ban.csv')['thanh tien'].sum()}"
    assert run_code(ok, path, "2026-01-05") == {"answer": {"type": "number", "answer": 35000}}
    bad = run_code("x = table('ban.csv')['Doanh thu']", path, "2026-01-05")
    assert "Thành tiền" in bad["error"] and "Trạng thái" in bad["error"]


def test_table_answers_and_thinking_output_are_normalised(tmp_path):
    root = _split(tmp_path)
    records = (
        "<think>table('ban.csv') rồi ```python\nANSWER = 1\n```</think>\n"
        "```python\nANSWER = {'type': 'table', 'answer': "
        "[{'mon': 'Cà phê', 'tien': 20000}, {'mon': 'Trà', 'tien': 15000}]}\n```"
    )
    agent = CodeAgent(FakeModel([records] * 3), root, tmp_path / "c", min_votes=3)
    out = agent.answer(Question("q1", "s1", "?", "2026-01-05"))
    assert out["answer"]["answer"] == {
        "columns": ["mon", "tien"],
        "rows": [["Cà phê", 20000], ["Trà", 15000]],
    }
    assert extract_code("<think>chưa xong ```python\nANSWER = 1\n```") is None


def test_code_only_planner_generates_a_chunk_in_one_batch(tmp_path):
    root = _split(tmp_path)
    code = "```python\nANSWER = {'type': 'number', 'answer': 5}\n```"

    class Batch(FakeModel):
        calls = 0

        def sample_batch(self, conversations, timeout, **kwargs):
            Batch.calls += 1
            return [{"texts": self.texts, "usage": {}} for _ in conversations]

    agent = CodeAgent(Batch([code] * 3), root, tmp_path / "c", min_votes=3)

    class Base:
        name, metadata = "base", {}

    planner = FallbackPlanner(Base(), agent, skip_base=True, chunk=10)
    questions = [Question(f"q{i}", "s1", "?", "2026-01-05") for i in range(4)]
    planner.plan_ahead(questions)
    answers = [planner.decide(q, context=object())["answer"] for q in questions]
    assert [a["answer"] for a in answers] == [5, 5, 5, 5] and Batch.calls == 1


def test_rulebook_helpers_for_periods_rounding_and_lunar_dates(tmp_path):
    path = tmp_path / "t.pkl"
    calendar = pd.DataFrame(
        {
            "date": ["2026-02-16", "2026-02-17"],
            "weekday": [1, 2],
            "lunar_day": [29, 1],
            "lunar_month": [12, 1],
            "lunar_year": [2025, 2026],
            "lunar_leap": ["false", "false"],
            "holiday": ["", ""],
        }
    )
    pickle.dump({"__raw__": {}, "__clean__": {}, "__calendar__": calendar}, path.open("wb"))
    code = (
        "ANSWER = {'type': 'list', 'answer': [round_half_up(2.5), round_half_up(2.45, 1), "
        "str(week_range(date(2026, 11, 18), 'sun')[0]), str(week_range(date(2026, 11, 18))[0]), "
        "str(month_range(2026, 2)[1]), str(last_n_days(7, YESTERDAY)[0]), "
        "str(lunar_to_solar(1, 1, 2026)), list(solar_to_lunar('2026-02-16'))]}"
    )
    out = run_code(code, path, "2026-11-20")
    assert out == {
        "answer": {
            "type": "list",
            "answer": [
                3,
                2.5,
                "2026-11-15",
                "2026-11-16",
                "2026-02-28",
                "2026-11-13",
                "2026-02-17",
                [29, 12, 2025, False],
            ],
        }
    }
