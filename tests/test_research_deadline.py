import pytest, time
from atira_agent import runtime
from atira_agent.contracts import RuntimeBudget

@pytest.mark.asyncio
async def test_research_deadline(monkeypatch):
    seen = []
    async def fake_investigate(*args, **kwargs):
        seen.append(1)
        return {"status": "complete", "records": []}
    monkeypatch.setattr(runtime, "_investigate", fake_investigate)
    budget = RuntimeBudget(limit_seconds=1200)
    budget.started = time.monotonic() - 601
    brief = {}
    sites = [{"site_id": "synthetic"}]
    await runtime._investigate_all(sites, {}, brief, {}, None, budget)
    assert seen == []
    await runtime._investigate_second_wave(
        sites, [{"site_id": "synthetic", "focus": "missing cost"}], brief, {}, None, budget
    )
    assert seen == []
