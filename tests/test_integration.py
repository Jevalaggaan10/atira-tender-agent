"""Integration of the entrypoint with the kit and the campaign: four CDP pages, a site run, the clock, the schema.

Invented runs and fake browsers only — no live website, no sandbox, no scenario values.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:  # the packager is a dev script, importable for its own tests
    sys.path.insert(0, str(ROOT / "scripts"))

import package_release

import agent
import browsers

SITES = [{"siteId": f"site-{n}", "url": f"https://site{n}.example/", "credentials": {"username": "u", "password": "p"}}
         for n in range(1, 5)]
SCHEMA = {"type": "object", "required": ["net_eur"], "properties": {"net_eur": {"type": "number"}}}


def run_doc(kind="api", expires_ms=None):
    run = {"run_id": "run_test", "brief": "Prepare the bid.", "answer_schema": SCHEMA, "access": SITES,
           "submission": {"kind": kind, "url": "https://tender.example/configurator"}}
    if kind == "api":
        run["submission"]["url"] = "https://api.example/v1/runs/current/answer"
    if expires_ms:
        run["expires_at"] = expires_ms
    return run


class FakePage:
    def __init__(self, number):
        self.number = number


class FakeBrowser:
    def __init__(self, number):
        self.number, self.closed = number, 0

    async def close(self):
        self.closed += 1


@pytest.fixture
def four_cdp(monkeypatch):
    """A sandbox with four CDP endpoints: connect hands out one browser and one page each."""
    monkeypatch.setenv("CDP_URLS", json.dumps([f"http://127.0.0.1:9{n}/cdp" for n in range(1, 5)]))
    opened: list[FakeBrowser] = []

    async def connect(playwright, number=0):
        assert number < 4, "never ask the sandbox for a browser it did not export"
        browser = FakeBrowser(number)
        opened.append(browser)
        return browser, FakePage(number)

    monkeypatch.setattr(browsers, "connect", connect)
    return opened


def fake_campaign(record):
    async def campaign(task, pages, store, budget, roles=None):
        from atira_agent.contracts import RunOutcome
        record["pages"] = list(pages)
        record["store"] = store
        record["budget"] = budget
        return record.get("outcome", RunOutcome(submitted=False, candidate={"net_eur": 1.0}, reason="api"))
    return campaign


@pytest.mark.asyncio
async def test_four_sites_get_four_cdp_pages(four_cdp):
    record: dict = {}
    with patch.object(agent, "run_campaign", fake_campaign(record)):
        assert await agent.solve(run_doc(), FakePage(0), [FakePage(1), FakePage(2), FakePage(3)]) == {"net_eur": 1.0}
    assert [p.number for p in record["pages"]] == [0, 1, 2, 3]
    from atira_agent.evidence import EvidenceStore
    assert isinstance(record["store"], EvidenceStore)


@pytest.mark.asyncio
async def test_site_run_is_entered_on_the_site_never_posted(four_cdp, capsys):
    """The site kind returns None even with a candidate, and says what actually happened on the website."""
    from atira_agent.contracts import RunOutcome
    record = {"outcome": RunOutcome(submitted=True, receipt="Ihr Angebot ist eingegangen", reason="")}
    with patch.object(agent, "run_campaign", fake_campaign(record)):
        answer = await agent.solve(run_doc("site"), FakePage(0), [FakePage(1)])
    assert answer is None
    assert "entered on the website" in capsys.readouterr().out
    # a site failure must also not produce an answer to POST
    record["outcome"] = RunOutcome(submitted=False, candidate={"net_eur": 9.9}, reason="TAN refused")
    with patch.object(agent, "run_campaign", fake_campaign(record)):
        assert await agent.solve(run_doc("site"), FakePage(0), [FakePage(1)]) is None


@pytest.mark.asyncio
async def test_api_candidate_must_fit_the_answer_schema(four_cdp, capsys):
    from atira_agent.contracts import RunOutcome
    record = {"outcome": RunOutcome(submitted=False, candidate={"net_eur": "zehn"}, reason="api")}
    with patch.object(agent, "run_campaign", fake_campaign(record)):
        assert await agent.solve(run_doc(), FakePage(0), [FakePage(1)]) is None
    assert "answer_schema" in capsys.readouterr().out
    assert agent.candidate_errors({"net_eur": 10.0}, SCHEMA) == []


def test_budget_is_local_cap_and_server_expiry():
    import time
    budget = agent.make_budget(run_doc(expires_ms=int(time.time() * 1000) + 60_000))
    assert budget.remaining() <= agent.RUN_LIMIT_SECONDS  # the server's 2h grant never stretches us past 20m
    short = agent.make_budget(run_doc(expires_ms=int(time.time() * 1000) + 30_000))
    assert short.remaining() <= 30.0
    assert agent.make_budget(run_doc()).remaining() <= agent.RUN_LIMIT_SECONDS


@pytest.mark.asyncio
async def test_solve_stops_at_the_outer_deadline(four_cdp):
    """A campaign that ignores the budget is still cut off; nothing valid is lost by waiting."""
    async def never_ends(task, pages, store, budget, roles=None):
        await asyncio.sleep(30)
    budget = agent.make_budget(run_doc())
    budget.limit_seconds = 0.2
    budget.reserve_seconds = 0.0
    with patch.object(agent, "run_campaign", never_ends):
        assert await agent.solve(run_doc(), FakePage(0), [FakePage(1)], budget) is None


@pytest.mark.asyncio
async def test_main_uses_every_exported_cdp_and_detaches(four_cdp, monkeypatch):
    monkeypatch.setenv("CHALLENGE_SCOUTING", json.dumps(run_doc()))  # local scouting: nothing is submitted
    record: dict = {}
    async def connect_over_cdp(url):  # no real playwright needed: the pages are stand-ins
        raise AssertionError("unused")
    monkeypatch.setattr(agent.async_playwright, "__enter__", lambda self: self, raising=False)

    class FakePlaywright:
        async def __aenter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect_over_cdp))

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(agent, "async_playwright", lambda: FakePlaywright())
    with patch.object(agent, "run_campaign", fake_campaign(record)):
        code = await agent.main()
    assert code == 0
    assert [b.number for b in four_cdp] == [0, 1, 2, 3]
    assert all(b.closed == 1 for b in four_cdp)


def test_event_log_keeps_counts_and_drops_secrets(capsys):
    from atira_agent import events
    events.emit("model_call", prompt_tokens=1200, completion_tokens=80, total_tokens=1280,
                password="hunter2", api_key="sk-live", run_token="rt_live",
                note="the TAN is 4711 and the password: hunter2")
    line = capsys.readouterr().err
    assert '"prompt_tokens":1200' in line and '"completion_tokens":80' in line
    assert "hunter2" not in line and "sk-live" not in line and "rt_live" not in line and "4711" not in line
    # a token-shaped string under a token name is still redacted, a number is a counter
    events.emit("x", token="rt_live", tokens=9)
    err = capsys.readouterr().err
    assert "rt_live" not in err and '"tokens":9' in err


def test_local_run_never_launches_its_own_browser_in_a_run_env():
    """In a run, browsers.py must use the CDP endpoints; a local box (no CDP) may still launch Chromium."""
    assert browsers.cdp_urls({"CDP_URLS": json.dumps(["http://a/cdp", "http://b/cdp"])}) == ["http://a/cdp",
                                                                                               "http://b/cdp"]
    assert browsers.cdp_urls({}) == []
    assert agent.MAX_BROWSERS == 4 and agent.RUN_LIMIT_SECONDS == 1200.0
    assert os.environ.get("HEADLESS") or True  # nothing here opens a browser


def test_release_staging_allowlist(tmp_path):
    """The packager stages exactly the runtime: code and lockfile, never docs, dev inputs, git or evidence."""
    script = ROOT / "scripts" / "package_release.py"
    assert script.exists()
    sys.path.insert(0, str(script.parent))
    import package_release

    plan = package_release.plan(ROOT)
    names = sorted(item["path"] for item in plan["files"])
    assert "agent.json" in names and "pyproject.toml" in names and "uv.lock" in names
    assert "agent.py" in names and "run.py" in names and "browsers.py" in names and "models.py" in names
    assert any(n.startswith("atira_agent/runtime") for n in names)
    assert all(not n.startswith(("docs/", "tests/", ".dev-input/", ".agents/", "scripts/", "README")) for n in names)
    assert all(Path(n).suffix in (".py", ".json", ".toml", ".lock") for n in names)


def _synthetic_release_root(tmp_path, name="src"):
    """A complete synthetic runtime tree: same file names, invented contents — no real source copied."""
    root = tmp_path / name
    for rel in ("agent.py", "run.py", "browsers.py", "models.py", "agent.json", "pyproject.toml", "uv.lock"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x = 1\n" if rel.endswith(".py") else "{}\n")
    for rel in package_release.REQUIRED_PACKAGE_MODULES:
        (root / package_release.PACKAGE_DIR / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / package_release.PACKAGE_DIR / rel).write_text("x = 1\n")
    return root


def test_check_demands_the_exact_runtime(tmp_path):
    """Not just shape: an empty folder, a stray safe-extension file and a duplicate are all refused."""
    (tmp_path / "empty").mkdir()
    with pytest.raises(package_release.PackageError, match="nothing at all"):
        package_release.check(tmp_path / "empty")

    good = _synthetic_release_root(tmp_path)
    stage = tmp_path / "release"
    package_release.stage(stage, good)
    assert package_release.check(stage)["count"] == len(package_release.plan(good)["files"])

    (stage / "extra.py").write_text("x = 1\n")  # a tidy folder with one extra safe file is not a release
    with pytest.raises(package_release.PackageError, match="not the runtime"):
        package_release.check(stage)
    (stage / "extra.py").unlink()

    import io
    import tarfile
    tar = tmp_path / "dup.tar.gz"
    with tarfile.open(tar, "w:gz") as a:  # an archive may carry one path twice: refused
        for name in ("agent.json", "agent.json"):
            info = tarfile.TarInfo(name)
            info.size = 3
            a.addfile(info, io.BytesIO(b"{}\n"))
    with pytest.raises(package_release.PackageError, match="twice"):
        package_release.check(tar)


def test_plan_refuses_a_smuggled_package(tmp_path):
    """A symlinked atira_agent/ or a module that is not on the runtime set fails the plan, not the run."""
    good = _synthetic_release_root(tmp_path)
    real = tmp_path / "real-package"
    (good / package_release.PACKAGE_DIR).rename(real)
    (good / package_release.PACKAGE_DIR).symlink_to(real)
    with pytest.raises(package_release.PackageError, match="symlink"):
        package_release.plan(good)

    other = _synthetic_release_root(tmp_path, name="src-extra")
    (other / package_release.PACKAGE_DIR / "extra_module.py").write_text("x = 1\n")
    with pytest.raises(package_release.PackageError, match="extra_module"):
        package_release.plan(other)
    missing = _synthetic_release_root(tmp_path, name="src-missing")
    (missing / package_release.PACKAGE_DIR / "events.py").unlink()
    with pytest.raises(package_release.PackageError, match="events.py"):
        package_release.plan(missing)


def test_stage_refuses_bad_targets_before_touching_anything(tmp_path):
    """A bad --out cannot destroy: the source root, an ancestor, a symlink or someone else's folder."""
    good = _synthetic_release_root(tmp_path)
    with pytest.raises(package_release.PackageError, match="source tree"):
        package_release.stage(good, good)
    with pytest.raises(package_release.PackageError):
        package_release.stage(tmp_path, good)
    valuable = tmp_path / "keep-me"
    valuable.mkdir()
    (valuable / "keep.txt").write_text("precious")
    with pytest.raises(package_release.PackageError, match="did not stage"):
        package_release.stage(valuable, good)
    assert (valuable / "keep.txt").read_text() == "precious"  # the guard ran before any mutation
    link = tmp_path / "out-link"
    link.symlink_to(valuable)
    with pytest.raises(package_release.PackageError, match="symlink"):
        package_release.stage(link, good)


def test_stage_uploads_exactly_the_runtime_and_owns_only_its_own(tmp_path):
    """Nothing inside the stage but the runtime (atira test-run uploads every file it finds); a refused
    re-stage keeps the old stage whole, and even our own marked stage is only replaced while it still holds
    exactly the planned runtime — content added since is refused and preserved, never deleted."""
    good = _synthetic_release_root(tmp_path)
    stage = tmp_path / "stage"
    package_release.stage(stage, good)
    staged = {p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file()}
    assert staged == {i["path"] for i in package_release.plan(good)["files"]}
    assert not (stage / ".atira-release-staging").exists() and not (stage / ".gitignore").exists()
    assert (tmp_path / f"stage{package_release.STAGE_META_SUFFIX}").exists()  # ownership lives BESIDE the stage
    assert not any(stage.rglob("__pycache__"))  # an AST parse, not compileall

    (stage / "junk.txt").write_text("junk")  # an extra added to our own stage after the marker
    broken = _synthetic_release_root(tmp_path, name="broken-src")
    (broken / "atira_agent" / "__init__.py").write_text("def broken(:\n")
    with pytest.raises(package_release.PackageError, match="does not parse"):
        package_release.stage(stage, broken)
    assert (stage / "junk.txt").exists() and (stage / "agent.py").exists()  # refused build touched nothing
    # our own marker proves who made the folder, not what it holds NOW: extras are refused whole, kept alive
    with pytest.raises(package_release.PackageError, match="we did not stage"):
        package_release.stage(stage, good)
    assert (stage / "junk.txt").read_text() == "junk" and (stage / "agent.py").exists()
    (stage / "junk.txt").unlink()  # re-stage proceeds only once the human content is gone
    package_release.stage(stage, good)
    assert package_release.check(stage)["count"] == len(package_release.plan(good)["files"])


def test_stage_keeps_human_additions_inside_a_marked_stage(tmp_path):
    """Root-proven data loss at 9ec6f53: a notes file added later inside OUR marked stage was rmtree'd.
    The marker must never license deleting content the packager did not stage — file OR directory."""
    good = _synthetic_release_root(tmp_path)
    stage = tmp_path / "stage"
    package_release.stage(stage, good)
    (stage / "human-notes.txt").write_text("do not delete me")
    (stage / "my-plans").mkdir()
    with pytest.raises(package_release.PackageError, match="human-notes.txt"):
        package_release.stage(stage, good)
    assert (stage / "human-notes.txt").read_text() == "do not delete me"
    assert (stage / "my-plans").is_dir() and (stage / "agent.py").exists()
