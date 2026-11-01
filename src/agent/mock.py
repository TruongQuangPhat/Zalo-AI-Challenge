"""Explicitly abstaining mock; no lookup by question ID or business ID."""

from src.data.split import Question


class MockPlanner:
    name = "mock-abstain"
    is_mock = True

    def decide(self, question: Question) -> dict:
        return {
            "answer": {"id": question.id, "type": "abstain"},
            "plan": {"status": "unsupported", "reason": "phase0_mock_no_execution"},
        }
