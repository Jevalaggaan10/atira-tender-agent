#!/usr/bin/env python3
"""Atira worker loop: adopt/monitor/review/refine Qwen worker sessions.

Master-only control tool. It never codes worker-owned files and never merges.

Subcommands
  status              snapshot all roles, refresh .dev-input/master-status.json
  watch               poll until every role is terminal or --timeout expires
  gate                4th REVIEW worktree on base + cherry-pick worker commits
                      + control commit, run combined suite on the union
  resume --role R     continue an EXISTING worker session with a feedback file;
                      refuses while the role is alive (queues instead — never a
                      second actor on one session id, even with --force)
  launch --role R     fresh worker ONLY if dead and unreported (guarded)
  review              independent medium-budget Qwen reviewer on the gate diff
                      (dedicated unprimed agent, no LOGBOOK seeding)
  prompt-save         capture live worker prompts from ps into .dev-input

Budget policy (human 2026-10-10): workers --variant fast, thinking off,
OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=8192; master/reviewers medium,
thinking, 16384. Explicit per-call budgets, not native reasoning levels.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODEL = "atira/qwen3.8-flash-next"
REPO_SLUG = os.environ.get("ATIRA_REPO_SLUG", "Jevalaggaan10/atira-tender-agent")
PLAN = REPO / ".dev-input" / "worktree-plan.json"
BOOT = REPO / ".dev-input" / "bootstrap-workers.json"
STATUS = REPO / ".dev-input" / "master-status.json"
RUNS = REPO / ".dev-runs" / "worker-loop"
QUEUE = RUNS / "queue"
WORKER_BUDGET = {"variant": "fast", "output_max": "8192"}
REVIEW_BUDGET = {"variant": "medium", "output_max": "16384"}
TERMINAL = {"pr_open", "reported_dead", "dead_no_report", "dead_committed_no_pr",
            "merged"}
IDLE_LOG_S = 900
PR_TTL_S = 60


def run(cmd, cwd=None, timeout=120, env=None):
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, timeout=timeout,
                          env=env, capture_output=True, text=True, check=False)


def git(cwd, *args, timeout=120):
    return run(["git", "-C", str(cwd), *args], timeout=timeout)


def plan():
    return json.loads(PLAN.read_text())


def boot():
    return json.loads(BOOT.read_text())


# ---------------------------------------------------------------- state

def state_file():
    RUNS.mkdir(parents=True, exist_ok=True)
    return RUNS / "state.json"


def load_state():
    p = state_file()
    return json.loads(p.read_text()) if p.exists() else {}


def save_state(s):
    state_file().write_text(json.dumps(s, indent=2, sort_keys=True))


def ps_lines():
    return run(["ps", "-Ao", "pid=,command="]).stdout.splitlines()


def commits_between(base, branch, cwd):
    """Commit shas on branch since base (oldest first). One range argument."""
    r = git(cwd, "rev-list", "--reverse", f"{base}..{branch}")
    if r.returncode:
        return []
    return r.stdout.split()


def branch_files(base, branch, cwd):
    r = git(cwd, "diff", "--name-only", f"{base}..{branch}")
    return r.stdout.split() if r.returncode == 0 else []


def foreign_files(base, branch, cwd, owned):
    return sorted(set(branch_files(base, branch, cwd)) - set(owned))


def effective_sid(role, w, st):
    """Live session identity: a discovered fresh-launch sid supersedes the
    bootstrap sid (which may name a terminated session)."""
    rec = st.get("launches", {}).get(role) or {}
    return rec.get("session_id") or w["session_id"]


def cwd_of(pid):
    r = run(["lsof", "-a", "-d", "cwd", "-p", str(pid), "-Fn"], timeout=15)
    best = None
    for tok in r.stdout.split():
        if tok.startswith("n") and len(tok) > 1 and not tok.startswith("nn"):
            best = tok[1:]
    return best


def adopt(boot_data=None, lines=None, state=None):
    """Map roles to live PIDs: session-id match, then worktree-cwd match (catches
    fresh launches nobody registered), then resume/launch PIDs; never clobber."""
    b = boot_data or boot()
    st = state if state is not None else load_state()
    lines = lines if lines is not None else ps_lines()
    procs = {}
    for line in lines:
        if "opencode run" not in line:
            continue
        try:
            pid = int(line.strip().split(None, 1)[0])
        except (ValueError, IndexError):
            continue
        procs[pid] = line
    sid_pid, cwd_pid = {}, {}
    dirs = {str(Path(w["directory"]).resolve()): role
            for role, w in b["workers"].items()}
    for pid, line in procs.items():
        for role, w in b["workers"].items():
            for sid in {w["session_id"],
                        (st.get("launches", {}).get(role) or {}).get("session_id")}:
                if sid and f"--session {sid}" in line:
                    sid_pid[role] = pid
        cwd = cwd_of(pid)
        role = dirs.get(str(Path(cwd).resolve())) if cwd else None
        if role and (role not in cwd_pid or pid > cwd_pid[role]):
            cwd_pid[role] = pid
    pids = {}
    for role in b["workers"]:
        launched = st.get("launches", {}).get(role) or {}
        resumes = st.get("resumes", {}).get(role) or []
        chain = [sid_pid.get(role), cwd_pid.get(role)]
        if resumes:
            chain.append(resumes[-1].get("pid"))
        chain.append(launched.get("pid"))
        pids[role] = next((p for p in chain if p and alive(p)), None)
    st["pids"] = pids
    st["adopted_at"] = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    if state is not None:
        return pids
    save_state(st)
    return pids


def discover_sids(st=None):
    """Parse session ids that fresh launches created, from their JSON logs."""
    st = st if st is not None else load_state()
    changed = False
    for rec in st.get("launches", {}).values():
        logp = Path(rec.get("log", ""))
        if not logp.exists():
            continue
        sid = rec.get("session_id")
        for line in logp.read_text(errors="replace").splitlines():
            if '"sessionID"' in line:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                s = ev.get("sessionID") or (ev.get("part") or {}).get("sessionID")
                if s and s.startswith("ses_"):
                    sid = s
        if sid and sid != rec.get("session_id"):
            rec["session_id"] = sid
            changed = True
    if changed:
        save_state(st)
    return st


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


def resolve_log(role, w, st=None):
    """Active log for a role: newest resume/launch log, then bootstrap log,
    then the fast-v2 log beside the worktrees root."""
    st = st if st is not None else load_state()
    wt_root = Path(w["directory"]).parent.parent  # .../work
    cands = []
    lrec = st.get("launches", {}).get(role)
    if isinstance(lrec, dict) and lrec.get("log"):
        cands.append(Path(lrec["log"]))
    resumes = st.get("resumes", {}).get(role) or []
    if resumes and isinstance(resumes[-1], dict):
        cands.insert(0, Path(resumes[-1]["log"]))
    cands.append(Path(w["log"]))
    cands.append(wt_root / f"qwen-{role}-fast-v2.jsonl")
    for c in cands:
        try:
            if c.exists():
                return c
        except OSError:
            continue
    return cands[0]


# ---------------------------------------------------------------- views

class PRCache:
    def __init__(self, ttl=PR_TTL_S, clock=time.time):
        self.ttl, self.clock, self.mem = ttl, clock, {}

    def get(self, role, branch, repo_slug):
        ent = self.mem.get(role)
        now = self.clock()
        if ent and now - ent["ts"] < self.ttl:
            return ent["pr"]
        r = run(["gh", "pr", "list", "--repo", repo_slug, "--head", branch,
                 "--state", "all", "--json", "number,url,state"], timeout=45)
        pr = None
        if r.returncode == 0:
            try:
                lst = json.loads(r.stdout or "[]")
                pr = lst[0] if lst else None
            except json.JSONDecodeError:
                pr = None
        self.mem[role] = {"ts": now, "pr": pr}
        return pr


_pr_cache = PRCache()


def role_view(role, base, owned, w, pids, cache, st=None):
    wt = Path(w["directory"])
    st = st if st is not None else load_state()
    pid = pids.get(role)
    lg = resolve_log(role, w, st)
    log_age = round(time.time() - lg.stat().st_mtime, 1) if lg.exists() else None
    wt_age = None  # seconds since any owned/visible source file changed
    for sub in ("atira_agent", "tests", "agent.py", "scripts"):
        target = wt / sub
        if not target.exists():
            continue
        files = [target] if target.is_file() else list(target.rglob("*.py"))
        for f in files:
            try:
                age = round(time.time() - f.stat().st_mtime, 1)
            except OSError:
                continue
            if wt_age is None or age < wt_age:
                wt_age = age
    shas = commits_between(base, w["branch"], wt)
    foreign = foreign_files(base, w["branch"], wt, owned)
    report_p = wt / ".dev-input" / "worker-report.json"
    report = None
    if report_p.exists():
        try:
            report = json.loads(report_p.read_text())
        except json.JSONDecodeError:
            report = {"error": "invalid json"}
    queued = sorted(QUEUE.glob(f"{role}-*.md")) if QUEUE.exists() else []
    view = {
        "session": effective_sid(role, w, st), "bootstrap_session": w["session_id"],
        "branch": w["branch"], "worktree": str(wt),
        "pid": pid, "alive": alive(pid), "log": str(lg), "log_age_s": log_age,
        "worktree_age_s": wt_age, "worker_commits": shas,
        "foreign_branch_files": foreign, "report": report,
        "queued_feedback": [q.name for q in queued],
    }
    pr = cache.get(role, w["branch"], REPO_SLUG) if (shas or report) else None
    view["pr"] = pr
    if pr and pr.get("state", "OPEN").upper() == "OPEN":
        view["state"] = "pr_open"
    elif report:
        view["state"] = "reported_dead" if not view["alive"] else "reported_running"
    elif view["alive"]:
        quiet = min(x for x in (log_age if log_age is not None else 1e9,
                                wt_age if wt_age is not None else 1e9))
        view["state"] = "idle_suspect" if quiet > IDLE_LOG_S else "running"
    elif shas:
        view["state"] = "dead_committed_no_pr"
    else:
        view["state"] = "dead_no_report"
    return view


def origin_heads():
    r = git(REPO, "ls-remote", "--heads", "origin", "refs/heads/dev/*")
    if r.returncode:
        return None
    names = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            names.append(parts[1])
    return sorted(names)


def snapshot():
    p, b = plan(), boot()
    base = p["base"]
    adopt()
    st = discover_sids()
    views = {role: role_view(role, base, p["ownership"][role], b["workers"][role],
                             st.get("pids", {}), _pr_cache, st)
             for role in p["worktrees"]}
    heads = origin_heads()
    out = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base": base,
        "budgets": {"workers": WORKER_BUDGET, "master_review": REVIEW_BUDGET},
        "workers": views,
        "origin_dev_heads": heads,
        "verified_facts": {
            "open_prs": sum(1 for v in views.values() if v.get("pr")),
            "origin_dev_branches": heads or [],
            "scored_submissions": "none attempted; team unranked until an "
                                  "eligible scored run exists",
        },
    }
    STATUS.write_text(json.dumps(out, indent=2, sort_keys=True))
    return out


# ---------------------------------------------------------------- commands

def cmd_status(_args):
    snap = snapshot()
    for role, v in snap["workers"].items():
        print(f"{role:12s} {v['state']:20s} pid={v['pid']} alive={v['alive']} "
              f"commits={len(v['worker_commits'])} log_age={v['log_age_s']}s "
              f"wt_age={v['worktree_age_s']}s "
              f"pr={v['pr']['url'] if v.get('pr') else '-'}")
    print(json.dumps(snap["verified_facts"], sort_keys=True))
    return snap


def cmd_watch(args):
    deadline = time.time() + args.timeout
    while True:
        snap = snapshot()
        states = {r: v["state"] for r, v in snap["workers"].items()}
        print(json.dumps({"t": time.strftime("%H:%M:%S"), **states}, sort_keys=True),
              flush=True)
        for role in snap["workers"]:
            action = drain_role(role, views=snap["workers"])
            if action:
                print(f"DRain {action}", flush=True)
        if all(s in TERMINAL for s in states.values()):
            print("ALL TERMINAL", flush=True)
            return snap
        if time.time() >= deadline:
            print("WATCH TIMEOUT", flush=True)
            return snap
        time.sleep(args.interval)


def cmd_prompt_save(_args):
    b = boot()
    out_dir = REPO / ".dev-input" / "worker-prompts"
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = {}
    for line in ps_lines():
        for role, w in b["workers"].items():
            sid = w["session_id"]
            if f"--session {sid}" not in line:
                continue
            try:
                argv = shlex.split(line.strip().split(None, 1)[1])
            except ValueError:
                continue
            try:
                i = argv.index("--format")
            except ValueError:
                continue
            msg = " ".join(argv[i + 2:])
            if msg.strip():
                f = out_dir / f"{role}-resume.txt"
                f.write_text(msg)
                saved[role] = str(f)
    print(json.dumps(saved, indent=2))


def spawn(cmd, cwd, logf, budget):
    env = os.environ.copy()
    env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"] = budget["output_max"]
    logf.parent.mkdir(parents=True, exist_ok=True)
    fh = logf.open("ab")
    p = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=fh,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    return p.pid


def cmd_launch(args):
    b = boot()
    role = args.role
    w = b["workers"][role]
    wt = Path(w["directory"])
    prompt_f = REPO / ".dev-input" / "worker-prompts" / f"{role}.txt"
    if not prompt_f.exists():
        sys.exit(f"missing prompt file {prompt_f}")
    snap = snapshot()
    v = snap["workers"][role]
    if v["alive"]:
        sys.exit(f"refuse: {role} pid {v['pid']} alive — resume, never duplicate")
    if v.get("pr"):
        sys.exit(f"refuse: {role} already has PR {v['pr']['url']}")
    if v.get("report") and not args.force:
        sys.exit(f"refuse: {role} already wrote a worker-report (--force to override)")
    ts = time.strftime("%Y%m%d-%H%M%S")
    logf = RUNS / f"{role}-launch-{ts}.jsonl"
    pid = spawn(["opencode", "run", "--dir", str(wt), "--agent", "orchestrator",
                 "--model", MODEL, "--variant", WORKER_BUDGET["variant"],
                 "--format", "json", prompt_f.read_text()], wt, logf, WORKER_BUDGET)
    st = load_state()
    st.setdefault("launches", {})[role] = {"pid": pid, "log": str(logf), "ts": ts,
                                           "started": time.time()}
    save_state(st)
    print(f"launched {role} pid={pid} log={logf} (sid will be discovered from log)")


def cmd_resume(args):
    b = boot()
    role = args.role
    w = b["workers"][role]
    msg_f = Path(args.message_file)
    if not msg_f.exists():
        sys.exit(f"missing {msg_f}")
    snap = snapshot()
    v = snap["workers"][role]
    QUEUE.mkdir(parents=True, exist_ok=True)
    if v["alive"]:
        # never a second actor on one session id — queue for master to deliver
        qf = QUEUE / f"{role}-{time.strftime('%Y%m%d-%H%M%S')}.md"
        qf.write_text(msg_f.read_text())
        print(f"queued feedback for {role} (pid {v['pid']} alive): {qf.name}")
        return
    ts = time.strftime("%Y%m%d-%H%M%S")
    logf = RUNS / f"{role}-resume-{ts}.jsonl"
    sid = effective_sid(role, w, load_state())
    pid = spawn(["opencode", "run", "--session", sid, "--agent",
                 "orchestrator", "--model", MODEL,
                 "--variant", WORKER_BUDGET["variant"], "--format", "json",
                 msg_f.read_text()], Path(w["directory"]), logf, WORKER_BUDGET)
    st = load_state()
    st.setdefault("resumes", {}).setdefault(role, []).append(
        {"pid": pid, "log": str(logf), "ts": ts})
    save_state(st)
    print(f"resumed {role} session={sid} pid={pid} log={logf}")


def drain_role(role, views=None, st=None):
    """Deliver ALL queued feedback to a DEAD worker exactly once, concatenated.
    Never injects into a live actor; never exceeds the 3-worker budget; files
    move to queue/consumed/ only AFTER the resume spawn succeeded."""
    b = boot()
    st = st if st is not None else load_state()
    if not QUEUE.exists():
        return None
    files = sorted(QUEUE.glob(f"{role}-*.md"))
    if not files or role not in b["workers"]:
        return None
    snap = {"workers": views} if views else snapshot()
    view = snap["workers"][role]
    state = view.get("state", "")
    if view["alive"] or state == "pr_open":
        return None
    if not (state.startswith("dead") or state == "reported_dead"):
        return None
    workers_alive = sum(1 for v in snap["workers"].values() if v["alive"])
    reviewer_alive = alive(st.get("review", {}).get("pid"))
    if workers_alive >= 3 or workers_alive + (1 if reviewer_alive else 0) >= 4:
        return f"{role}: queue held — actor budget full"
    msg = "\n\n".join(fp.read_text() for fp in files)
    ts = time.strftime("%Y%m%d-%H%M%S")
    tmp = RUNS / f"drain-{role}-{ts}.md"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(msg)
    try:
        cmd_resume(type("A", (), {"role": role, "message_file": str(tmp)})())
    except OSError as e:  # spawn failed: keep the queue intact
        return f"{role}: drain failed ({e}); queue kept"
    done = QUEUE / "consumed"
    done.mkdir(exist_ok=True)
    for fp in files:
        fp.rename(done / fp.name)
    return f"{role}: drained {len(files)} queued note(s) into one resume"


def cmd_gate(args):
    p, b = plan(), boot()
    base = p["base"]
    roles = args.roles.split(",")
    problems = []
    picks = []
    for role in roles:
        wt = Path(b["workers"][role]["directory"])
        owned = p["ownership"][role]
        foreign = foreign_files(base, b["workers"][role]["branch"], wt, owned)
        if foreign:
            problems.append(f"{role} touched NON-OWNED files: {foreign}")
        shas = commits_between(base, b["workers"][role]["branch"], wt)
        if not shas:
            problems.append(f"{role} has no commits yet")
        picks += shas  # oldest first
    if args.control_commit:
        picks.append(args.control_commit)
    if problems:
        print(json.dumps({"gate_blocked": problems}, indent=2))
        sys.exit(1)
    review_wt = Path(next(iter(p["worktrees"].values()))).parent / "review"
    ts = time.strftime("%Y%m%d-%H%M%S")
    st = load_state()
    managed = st.get("gate_worktree") == str(review_wt)
    if review_wt.exists():
        head = git(review_wt, "rev-parse", "HEAD").stdout.strip()
        detached = not git(review_wt, "branch", "--show-current").stdout.strip()
        # removal is safe only for a pristine gate product: detached HEAD,
        # recorded by a previous gate, and NO modifications beyond the .venv
        # link the gate itself created — anything else belongs to a human.
        dirty = [ln for ln in git(review_wt, "status", "--porcelain").stdout.splitlines()
                 if ln.strip() and "? .venv" not in ln and "?? .venv" not in ln]
        if not (args.reuse and managed and detached and not dirty):
            sys.exit(f"review worktree {review_wt} is not a pristine gate product "
                     f"(managed={managed} detached={detached} head={head[:8]} "
                     f"dirty={dirty[:3]}); refusing to remove it")
        rm = git(REPO, "worktree", "remove", "--force", str(review_wt))
        if rm.returncode:
            sys.exit(f"worktree remove failed: {rm.stderr}")
    r = git(REPO, "worktree", "add", "--detach", str(review_wt), base)
    if r.returncode:
        sys.exit(f"worktree add failed: {r.stderr}")
    st = load_state()
    st["gate_worktree"] = str(review_wt)
    save_state(st)
    venv = review_wt / ".venv"
    if not venv.exists():
        venv.symlink_to((REPO / ".venv").resolve())
    # Run tests with the project venv interpreter directly. Per primary docs
    # (uv sync "Handling of extraneous packages"), `uv run` performs an INEXACT
    # sync by default and does NOT remove extraneous packages — no deletion was
    # ever demonstrated; using the interpreter anyway is a determinism choice:
    # the gate stays free of any sync step against the shared symlinked venv.
    py = REPO / ".venv" / "bin" / "python"
    report = {"ts": ts, "base": base, "picked": picks, "commands": {}}
    cp = git(review_wt, "cherry-pick", *picks, timeout=300)
    report["cherry_pick"] = {"rc": cp.returncode, "stderr_tail": cp.stderr[-2000:]}
    if cp.returncode == 0:
        changed = [f for f in branch_files(base, "HEAD", review_wt)
                   if f.endswith(".py")]
        cmds = [("pytest", [str(py), "-m", "pytest", "-q"])]
        if changed:
            # ruff over the CHANGED files only: pre-existing base-tree lint is
            # reported as info elsewhere, the gate must fail on what we added.
            cmds.append(("ruff_changed", [str(py), "-m", "ruff", "check", *changed]))
        for name, cmd in cmds:
            g = run(cmd, cwd=review_wt, timeout=args.gate_timeout)
            report["commands"][name] = {"rc": g.returncode,
                                        "stdout_tail": g.stdout[-3000:],
                                        "stderr_tail": g.stderr[-1500:]}
    report["gate_pass"] = (report["cherry_pick"]["rc"] == 0 and bool(report["commands"])
                           and all(c.get("rc") == 0 for c in report["commands"].values()))
    (RUNS / f"gate-{ts}.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({"gate_pass": report["gate_pass"],
                      "cherry_pick_rc": report["cherry_pick"]["rc"],
                      "commands": {k: v.get("rc") for k, v in report["commands"].items()},
                      "picked": picks}, indent=2))
    print(f"gate report: {RUNS / f'gate-{ts}.json'} (worktree kept for inspection)")
    sys.exit(0 if report["gate_pass"] else 1)


def cmd_review(args):
    gate_files = sorted(RUNS.glob("gate-*.json"))
    if not gate_files:
        sys.exit("run gate first")
    g = json.loads(gate_files[-1].read_text())
    p = plan()
    review_wt = Path(next(iter(p["worktrees"].values()))).parent / "review"
    diff = git(review_wt, "diff", f"{p['base']}..HEAD").stdout
    ts = time.strftime("%Y%m%d-%H%M%S")
    difff = RUNS / f"review-{ts}"
    difff.mkdir(parents=True, exist_ok=True)
    (difff / "combined.diff").write_text(diff)
    (difff / "gate-report.json").write_text(json.dumps(g, indent=2))
    prompt = (
        "You are an UNPRIMED independent senior Qwen reviewer (medium budget, "
        "thinking on). Do NOT read LOGBOOK.md, prior review notes or run logs. "
        f"Read AGENTS.md and the COMPLETE official rules in "
        f"{REPO / '.dev-input' / 'official-materials.txt'}. Review the combined "
        f"change set in {difff}/combined.diff against those rules: architecture, "
        "model-directed runtime (no scripted paths, no fabricated facts, real "
        "deadline reserve, honest candidate handling), pricing correctness, "
        "evidence guards, packaging/staging safety, and whether tests are "
        f"meaningful synthetic checks. Verify by targeted offline runs in "
        f"{review_wt} (use .venv/bin/python -m pytest on specific files). Do "
        f"NOT edit files. Write {difff}/review-findings.json: concrete problems "
        "each with file:line, rule violated, severity, specific fix; plus an "
        "honest ok list. Demonstrated issues only; no confidence scores."
    )
    logf = RUNS / f"review-{ts}.jsonl"
    pid = spawn(["opencode", "run", "--agent", "reviewer", "--model", MODEL,
                 "--variant", REVIEW_BUDGET["variant"], "--format", "json",
                 "--pure", prompt], REPO, logf, REVIEW_BUDGET)
    st = load_state()
    st["review"] = {"pid": pid, "log": str(logf), "dir": str(difff)}
    save_state(st)
    print(f"reviewer pid={pid} log={logf} findings->{difff / 'review-findings.json'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    w = sub.add_parser("watch")
    w.add_argument("--interval", type=int, default=30)
    w.add_argument("--timeout", type=int, default=600)
    w.set_defaults(fn=cmd_watch)
    sub.add_parser("prompt-save").set_defaults(fn=cmd_prompt_save)
    l = sub.add_parser("launch")
    l.add_argument("--role", required=True)
    l.add_argument("--force", action="store_true")
    l.set_defaults(fn=cmd_launch)
    r = sub.add_parser("resume")
    r.add_argument("--role", required=True)
    r.add_argument("--message-file", required=True)
    r.set_defaults(fn=cmd_resume)
    g = sub.add_parser("gate")
    g.add_argument("--roles", default="runtime,core,integration")
    g.add_argument("--control-commit", default=None)
    g.add_argument("--reuse", action="store_true",
                   help="replace the review worktree only if gate-managed and detached")
    g.add_argument("--gate-timeout", type=int, default=900)
    g.set_defaults(fn=cmd_gate)
    sub.add_parser("review").set_defaults(fn=cmd_review)
    d = sub.add_parser("drain")
    d.add_argument("--role", default=None)
    d.set_defaults(fn=lambda a: [print(x) for x in
                                 [drain_role(r) for r in
                                  ([a.role] if a.role else list(boot()["workers"]))]
                                 if x])
    args = ap.parse_args()
    adopt()
    args.fn(args)


if __name__ == "__main__":
    main()
