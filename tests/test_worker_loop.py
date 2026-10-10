"""Controller tests: guards proven with temp Git repos and subprocess stubs.

These exercise scripts/worker_loop.py (master control), not the agent runtime.
Every test is offline: ps, gh, git-network and spawn are stubbed; real git
object operations run against throwaway temp repos.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import worker_loop as wl


def git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                       text=True, check=True)
    return r.stdout.strip()


@pytest.fixture()
def tmp_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "test")
    (repo / "owned.py").write_text("base\n")
    (repo / "other.py").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    git(repo, "branch", "dev/x-qwen")
    git(repo, "checkout", "dev/x-qwen")
    return repo


def commit_on(wt, path, msg, content=None):
    (wt / path).write_text(content if content is not None else f"{msg}\n")
    git(wt, "add", path)
    git(wt, "commit", "-m", msg)
    return git(wt, "rev-parse", "HEAD")


# -- range handling (root blocker: split ".." args are invalid) ----------

def test_commits_between_finds_real_commits(tmp_repo):
    base = git(tmp_repo, "rev-parse", "main")
    c1 = commit_on(tmp_repo, "owned.py", "c1")
    c2 = commit_on(tmp_repo, "owned.py", "c2")
    shas = wl.commits_between(base, "dev/x-qwen", tmp_repo)
    assert shas == [c1, c2]  # oldest first, real commits


def test_foreign_files_detects_nonowned(tmp_repo):
    base = git(tmp_repo, "rev-parse", "main")
    commit_on(tmp_repo, "other.py", "touched foreign")
    assert wl.foreign_files(base, "dev/x-qwen", tmp_repo, ["owned.py"]) == ["other.py"]
    assert wl.foreign_files(base, "dev/x-qwen", tmp_repo,
                            ["owned.py", "other.py"]) == []


# -- adoption identity (root blocker: never clobber fresh launch pids) ----

def _boot(sids):
    return {"workers": {r: {"session_id": s, "branch": f"dev/{r}-qwen",
                            "directory": "/wt", "log": "/dev/null"}
                        for r, s in sids.items()}}


def test_adopt_prefers_ps_match_then_live_launch_pid(monkeypatch):
    monkeypatch.setattr(wl, "REPO", Path("."))
    b = _boot({"runtime": "ses_old", "core": "ses_core"})
    st = {"launches": {"runtime": {"pid": 4242, "log": "/dev/null"}}}
    # old sid gone from ps; fresh launch pid alive
    monkeypatch.setattr(wl, "alive", lambda pid: pid == 4242)
    pids = wl.adopt(b, lines=["9999 opencode run unrelated"], state=st)
    assert pids["runtime"] == 4242
    # when ps shows the sid live, that identity wins
    monkeypatch.setattr(wl, "alive", lambda pid: True)
    pids = wl.adopt(b, lines=["7777 opencode run --session ses_old --format json"],
                    state=st)
    assert pids["runtime"] == 7777


# -- duplicate actor guards (root blocker) -------------------------------

def _fake_view(alive_flag, **kw):
    v = {"alive": alive_flag, "pid": 1 if alive_flag else None, "pr": None,
         "report": None, "worker_commits": [], "session": "ses_x",
         "branch": "dev/runtime-qwen", "worktree": "/wt", "state": "running"}
    v.update(kw)
    return v


def test_resume_alive_queues_without_spawning(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(wl, "boot", lambda: _boot({"runtime": "ses_x"}))
    monkeypatch.setattr(wl, "snapshot", lambda: {"workers": {
        "runtime": _fake_view(True)}})
    monkeypatch.setattr(wl, "spawn", lambda *a, **k: calls.append(a) or 1)
    q = tmp_path / "queue"
    monkeypatch.setattr(wl, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(wl, "QUEUE", q)
    msg = tmp_path / "fb.md"
    msg.write_text("do this")
    wl.cmd_resume(type("A", (), {"role": "runtime", "message_file": str(msg)})())
    assert calls == [], "alive role must never spawn a second actor"
    assert list(q.glob("runtime-*.md"))


def test_resume_dead_reuses_session_id(monkeypatch, tmp_path):
    spawned = []
    monkeypatch.setattr(wl, "snapshot", lambda: {"workers": {
        "runtime": _fake_view(False)}})
    monkeypatch.setattr(wl, "spawn",
                        lambda cmd, *a, **k: spawned.append(cmd) or 5)
    monkeypatch.setattr(wl, "boot", lambda: _boot({"runtime": "ses_x"}))
    monkeypatch.setattr(wl, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(wl, "load_state", dict)
    monkeypatch.setattr(wl, "save_state", lambda s: None)
    msg = tmp_path / "fb.md"
    msg.write_text("deliver")
    wl.cmd_resume(type("A", (), {"role": "runtime", "message_file": str(msg)})())
    assert spawned and "--session" in spawned[0]
    assert "ses_x" in spawned[0]


def test_launch_refused_for_alive_pr_and_report(monkeypatch, tmp_path):
    monkeypatch.setattr(wl, "boot", lambda: _boot({"runtime": "ses_x"}))
    (tmp_path / ".dev-input" / "worker-prompts").mkdir(parents=True)
    (tmp_path / ".dev-input" / "worker-prompts" / "runtime.txt").write_text("p")
    monkeypatch.setattr(wl, "REPO", tmp_path)
    spawned = []
    monkeypatch.setattr(wl, "spawn", lambda *a, **k: spawned.append(a) or 7)
    monkeypatch.setattr(wl, "load_state", dict)
    monkeypatch.setattr(wl, "save_state", lambda s: None)
    for view in (_fake_view(True),
                 _fake_view(False, pr={"url": "x", "state": "OPEN"}),
                 _fake_view(False, report={"ok": True})):
        monkeypatch.setattr(wl, "snapshot", lambda v=view: {"workers": {
            "runtime": v}})
        with pytest.raises(SystemExit):
            wl.cmd_launch(type("A", (), {"role": "runtime", "force": False})())
    assert spawned == [], "no fresh actor for alive/reported/PR-open roles"


# -- gate blocks foreign files BEFORE mutating anything ------------------

def test_gate_blocks_foreign_before_worktree(monkeypatch, tmp_repo, tmp_path):
    base = git(tmp_repo, "rev-parse", "main")
    commit_on(tmp_repo, "not_owned.py", "foreign edit")
    monkeypatch.setattr(wl, "plan", lambda: {
        "base": base,
        "worktrees": {"runtime": str(tmp_repo)},
        "ownership": {"runtime": ["owned.py"]}})
    monkeypatch.setattr(wl, "boot", lambda: {"workers": {"runtime": {
        "directory": str(tmp_repo), "branch": "dev/x-qwen",
        "session_id": "s", "log": "/dev/null"}}})
    seen = []
    orig_git = wl.git

    def spy(cwd, *a, **k):
        seen.append(a)
        return orig_git(cwd, *a, **k)

    monkeypatch.setattr(wl, "git", spy)
    with pytest.raises(SystemExit) as e:
        wl.cmd_gate(type("A", (), {"roles": "runtime", "control_commit": None,
                                   "gate_timeout": 60})())
    assert e.value.code == 1
    assert not any(a and a[0] == "worktree" for a in seen), \
        "gate must block on foreign files BEFORE creating or touching worktrees"


# -- fresh-state launch (state without a "launches" key) ------------------

def test_launch_from_fresh_state_records_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(wl, "boot", lambda: _boot({"runtime": "ses_x"}))
    (tmp_path / ".dev-input" / "worker-prompts").mkdir(parents=True)
    (tmp_path / ".dev-input" / "worker-prompts" / "runtime.txt").write_text("p")
    monkeypatch.setattr(wl, "REPO", tmp_path)
    monkeypatch.setattr(wl, "RUNS", tmp_path / "runs")
    saved = {}
    monkeypatch.setattr(wl, "load_state", dict)  # no "launches" key yet
    monkeypatch.setattr(wl, "save_state", lambda s: saved.update(s))
    monkeypatch.setattr(wl, "spawn", lambda *a, **k: 555)
    monkeypatch.setattr(wl, "snapshot", lambda: {"workers": {
        "runtime": _fake_view(False)}})
    wl.cmd_launch(type("A", (), {"role": "runtime", "force": False})())
    assert saved["launches"]["runtime"]["pid"] == 555


# -- queue drain (delivered once, consumed only after spawn success) ------

def _drain_env(monkeypatch, tmp_path, state, spawn_ok=True, views=None):
    monkeypatch.setattr(wl, "boot", lambda: _boot({"runtime": "ses_x"}))
    monkeypatch.setattr(wl, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(wl, "QUEUE", tmp_path / "queue")
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    wl.QUEUE.mkdir(parents=True, exist_ok=True)
    for stale in wl.QUEUE.glob("runtime-*"):
        stale.unlink()
    (wl.QUEUE / "runtime-a.md").write_text("FIRST")
    (wl.QUEUE / "runtime-b.md").write_text("SECOND")
    monkeypatch.setattr(wl, "load_state", dict)
    monkeypatch.setattr(wl, "save_state", lambda s: None)
    monkeypatch.setattr(wl, "snapshot", lambda: {"workers": views or {
        "runtime": _fake_view(False, state=state)}})
    calls = []

    def spawn(cmd, *a, **k):
        if not spawn_ok:
            raise OSError("no proxy")
        calls.append(cmd)
        return 9
    monkeypatch.setattr(wl, "spawn", spawn)
    return calls


def test_drain_concatenates_once_and_consumes_after_success(monkeypatch, tmp_path):
    calls = _drain_env(monkeypatch, tmp_path, "dead_committed_no_pr")
    msg = wl.drain_role("runtime")
    assert calls and "FIRST" in calls[0][-1] and "SECOND" in calls[0][-1]
    assert list(wl.QUEUE.glob("runtime-*.md")) == []
    assert len(list((wl.QUEUE / "consumed").glob("runtime-*.md"))) == 2
    assert "2 queued" in msg


def test_drain_keeps_queue_on_spawn_failure(monkeypatch, tmp_path):
    _drain_env(monkeypatch, tmp_path, "dead_no_report", spawn_ok=False)
    msg = wl.drain_role("runtime")
    assert "failed" in msg
    assert len(list(wl.QUEUE.glob("runtime-*.md"))) == 2


def test_drain_never_injects_alive_or_pr_open(monkeypatch, tmp_path):
    calls = _drain_env(monkeypatch, tmp_path, "running", views={
        "runtime": _fake_view(True)})
    assert wl.drain_role("runtime") is None and calls == []
    calls2 = _drain_env(monkeypatch, tmp_path, "pr_open", views={
        "runtime": _fake_view(False, pr={"url": "u", "state": "OPEN"})})
    assert wl.drain_role("runtime") is None and calls2 == []


# -- origin heads: names, not SHAs ----------------------------------------

def test_origin_heads_returns_ref_names(monkeypatch):
    out = "b9e0d648a75\trefs/heads/dev/core-qwen\n111\trefs/heads/dev/x-qwen\n"
    monkeypatch.setattr(wl, "git", lambda cwd, *a, **k: type(
        "R", (), {"returncode": 0, "stdout": out, "stderr": ""})())
    assert wl.origin_heads() == ["refs/heads/dev/core-qwen",
                                 "refs/heads/dev/x-qwen"]


# -- PR cache TTL ---------------------------------------------------------

def test_pr_cache_refreshes_none_after_ttl(monkeypatch):
    now = [1000.0]
    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        out = "[]" if len(calls) == 1 else '[{"number":1,"url":"u","state":"OPEN"}]'
        return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()
    monkeypatch.setattr(wl, "run", fake_run)
    c = wl.PRCache(ttl=60, clock=lambda: now[0])
    assert c.get("runtime", "b", "slug") is None
    assert c.get("runtime", "b", "slug") is None
    assert len(calls) == 1, "TTL must avoid hammering gh"
    now[0] += 61
    assert c.get("runtime", "b", "slug") == {"number": 1, "url": "u", "state": "OPEN"}
    assert len(calls) == 2, "a PR opened later must be discovered after TTL"


# -- log resolution (root blocker: worktrees parent vs work root) --------

def test_resolve_log_prefers_resume_then_bootstrap(tmp_path, monkeypatch):
    wt = tmp_path / "work" / "qwen-worktrees" / "runtime"
    wt.mkdir(parents=True)
    w = {"directory": str(wt), "log": str(tmp_path / "work" / "old-max.jsonl")}
    (tmp_path / "work" / "old-max.jsonl").write_text("")
    monkeypatch.setattr(wl, "load_state", dict)
    assert Path(wl.resolve_log("runtime", w)) == tmp_path / "work" / "old-max.jsonl"
    new = tmp_path / "runs" / "resume.jsonl"
    new.parent.mkdir()
    new.write_text("")
    monkeypatch.setattr(wl, "load_state",
                        lambda: {"resumes": {"runtime": [{"log": str(new)}]}})
    assert Path(wl.resolve_log("runtime", w)) == new
