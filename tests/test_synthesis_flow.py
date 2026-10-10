import json
import types

import pytest

import atira_agent.runtime as runtime
from atira_agent import pricing as runtime_pricing
from atira_agent.contracts import RuntimeBudget
from atira_agent.runtime import _synthesize

INLINE_MARKER = "INLINE_SNAPSHOT_MARKER_r1"
LIST = [{"rid": "r1", "text": INLINE_MARKER}]


@pytest.mark.asyncio
async def test_synthesize_priced_answer(monkeypatch):
    captured = {}

    class FakeQwenAgent:
        def __init__(self, name, system, tools, **kwargs):
            captured["system"] = system
            captured["tools"] = tools
            captured["kwargs"] = kwargs

        async def run(self, task, *, max_steps):
            captured["max_steps"] = max_steps
            for i in range(4):
                out = await captured["tools"]["evidence_snapshot"].fn()
                if i == 3:
                    assert isinstance(out, dict) and "error" in out
            await captured["tools"]["pricing_calculate"].fn(
                payload=json.dumps({"synthetic": True}))
            return {"error": "step_limit", "finished": False}

    monkeypatch.setattr(runtime, "QwenAgent", FakeQwenAgent)

    class Store:
        def snapshot(self, **kwargs):
            return LIST

        def stats(self):
            return {}

        def conflicts(self):
            return []

    pricing = types.SimpleNamespace(
        PricingError=runtime_pricing.PricingError,
        PAYLOAD_SCHEMA_DOC="synthetic",
        calculate_bid=lambda payload: {"answer": {"decision": "bid", "total_net": 42, "currency": "EUR"}},
    )
    budget = RuntimeBudget(limit_seconds=1200)
    out = await _synthesize("brief", {"type": "object"}, Store(), budget, "site",
                            pricing_module=pricing)
    assert out["answer"]["total_net"] == 42
    assert INLINE_MARKER in captured["system"]
    assert captured["max_steps"] == 14


@pytest.mark.asyncio
async def test_wrapper_finalizer_contract():
    """The runtime wrapper must not forward max_attempts to the actual delegate.

    On the old wrapper this raises TypeError (unexpected keyword 'max_attempts');
    with the fix it delegates cleanly and reports the honest outcome.
    """
    from atira_agent.runtime import _finalize_site, RuntimeBudget  # actual runtime module

    budget = RuntimeBudget(1200)
    outcome = await _finalize_site(
        "synthetic", {}, {"decision": "bid"}, [], {}, None, budget
    )

    # Actual delegate with sites=[] returns an unsubmitted RunOutcome.
    assert outcome.submitted is False
    assert "no tender portal in this run" in outcome.reason
