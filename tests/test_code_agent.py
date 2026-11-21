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
    code = "df = table('ban.csv')\nANSWER = {'type': 'number', 'answer': df['Tiền'].astype(int).sum()}"
    assert run_code(code, path, "2026-01-05") == {"answer": {"type": "number", "answer": 35000}}
    assert "error" in run_code("raise SystemExit(3)", path, "2026-01-05")


def test_extract_code_and_vote_key():
    assert extract_code("x\n```python\nANSWER = 1\n```") == "ANSWER = 1\n"
    assert extract_code("không có code") is None
    assert vote_key({"type": "number", "answer": 3}) == vote_key({"type": "number", "answer": 3.0})
    assert vote_key({"type": "string", "answer": " Trà "}) == vote_key({"type": "string", "answer": "trà"})


def test_table_helpers_clean_numbers_dates_and_join_sheets(tmp_path):
    path = tmp_path / "t.pkl"
    a = pd.DataFrame([["Ngày", "Tiền", "Mã"], ["1 thg 6 2025", "1.250.000", "0901234567"],
                      ["2/6/2025", "560,000", "0907654321"]])
    b = pd.DataFrame([["Ngày", "Tiền", "Mã"], ["3/7/2025", "12k", "0900000000"]])
    pickle.dump({"so.xlsx::T6": a, "so.xlsx::T7": b}, path.open("wb"))
    code = (
        "df = table('so.xlsx')\n"
        "ANSWER = {'type': 'list', 'answer': [df['Tiền'].sum(), str(df['Ngày'].min().date()), "
        "df['Mã'].iloc[0], len(df), sorted(df['sheet'].unique())]}"
    )
    out = run_code(code, path, "2026-01-05")
    assert out == {"answer": {"type": "list", "answer": [1822000, "2025-06-01", "0901234567", 3, ["T6", "T7"]]}}


def test_numeric_strings_are_coerced_and_failures_repaired(tmp_path):
    root = _split(tmp_path)
    as_text = "```python\nANSWER = {'type': 'number', 'answer': '1.250.000'}\n```"
    broken = "```python\nANSWER = {'type': 'number', 'answer': undefined_name}\n```"

    class Repairing(FakeModel):
        def sample(self, messages, timeout, **kwargs):
            if messages[-1]["role"] == "user" and "bị lỗi" in messages[-1]["content"]:
                return {"texts": [as_text, as_text], "usage": {}, "seconds": 0}
            return {"texts": self.texts, "usage": {}, "seconds": 0}

    agent = CodeAgent(Repairing([as_text, broken, broken, broken]), root, tmp_path / "c", min_votes=3)
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
