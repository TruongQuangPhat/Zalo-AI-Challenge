"""Test-only planner double for runtime durability, isolation and preparation tests."""


class StubPlanner:
    name = "test-planner"
    metadata = {"model": {"sha256": "0" * 64}}

    def decide(self, question, *, context=None):
        return {
            "answer": {"id": question.id, "type": "abstain"},
            "plan": {"status": "test_stub"},
        }
