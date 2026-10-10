import pytest
from atira_agent import runtime, contracts
from atira_agent.contracts import RuntimeBudget


@pytest.mark.asyncio
async def test_investigate_all_preserves_status_unresolved_blocker(monkeypatch):
    r1 = {"id": "rec-1"}

    async def _fake_investigate(site, missions, brief, answer_schema, all_sites, store, budget):
        return {
            "summary": "worker summary",
            "records": [r1],
            "status": "blocked",
            "unresolved": ["cost"],
            "blocker": "missing cost",
        }

    monkeypatch.setattr(runtime, "_investigate", _fake_investigate)

    sites = [{"site_id": "synthetic"}]
    budget = RuntimeBudget(1200)

    summaries = await runtime._investigate_all(
        sites, {}, "brief", {}, None, budget
    )

    assert len(summaries) == 1
    row = summaries[0]
    assert row["site"] == "synthetic"
    assert row["status"] == "blocked"
    assert row["unresolved"] == ["cost"]
    assert row["blocker"] == "missing cost"
    assert row["record_count"] == 1
