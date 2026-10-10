# LOGBOOK — Atira RFQ tender agent

Single-writer log: the master orchestrator alone updates this file. Workers and reviewers append nothing here;
they write machine-readable reports under their worktrees. High-level entries only: no scenario facts, values,
logins, transcripts or secrets. Entire captures sessions locally; its checkpoint refs are never pushed.
Keep this file bounded (~200 lines); deep history lives in Entire archives.

## Mission

Model-directed agent for the Atira RFQ challenge (4 German business websites → one priced, TAN-signed bid entered
on the tender portal). All development coding, orchestration and review through Atira-hosted Qwen only via the
local proxy; no paid fallback. Budget policy (human override 2026-10-10): parallel coding workers run the lowest
explicit budget — `--variant fast`, thinking disabled, output cap 8192; master and reviewers run medium —
thinking enabled, output cap 16384. These are explicit per-call budgets through the proxy, not native Qwen
reasoning_effort levels; context 262144. No human help inside runs, no hardcoded scenario facts, code-only
packages. The user explicitly authorized the public repo, this implementation and the worker-PR workflow; pushes
to `dev/*` and merges to main follow the root's final verification gate (no separate user permission needed).
Only a scored submission requires a separate explicit user approval at submission time.

## Durable workflow (user-authorized)

plan → parallel PR workers in isolated worktrees → master inspects actual diffs + tests → independent unprimed
Qwen review → bounded fixes → measured sandbox test runs (unranked first) → retain the best verified checkpoint.
Two failed repairs on one approach ⇒ change approach, never repeat unchanged. Honest gates only: recorded tests
and run results, never self-ratings. No verified bid exists yet.

- Worktrees: `.dev-input/worktree-plan.json` maps roles→paths under `work/qwen-worktrees/` (runtime, core,
  integration), each a git worktree on `dev/<role>-qwen` off base `b828da6` with the source snapshot
  (unowned files stay uncommitted). Workers commit ONLY owned files, push `HEAD:dev/<role>-qwen`, open a PR,
  and write `worker-report.json` (branch, pr_url, commit, exact test lines, limitations). No main push, no
  force-push, no merges by workers.
- Controller `scripts/worker_loop.py` (wrapper `scripts/launch_workers.sh`): adopts running worker sessions by
  the session ids in `.dev-input/bootstrap-workers.json`; refuses duplicate actors (resume while alive queues
  feedback instead of spawning; a fresh launch is refused for alive/reported/PR-open roles). `status`/`watch`
  track pids, log+worktree activity, commits, reports, PRs and rewrite `.dev-input/master-status.json`. `gate`
  creates a fourth REVIEW worktree on the base, blocks on any non-owned file in a branch diff, cherry-picks the
  worker commits (+ control commit) and runs the combined suite with `.venv/bin/python -m pytest/ruff` directly
  (no uv sync in the gate path; uv's default sync is inexact and does not remove extraneous packages — primary
  docs checked, no deletion was ever observed). `review` launches an unprimed reviewer (dedicated agent prompt,
  no LOGBOOK seeding). Timeout policy now: worker calls 180 s, master/reviewer 360 s (was 30 min leftover).
- Combined gate rule: never merge into the dirty main working tree; the review worktree is the integration
  testbed. Root performs final verification and remote merges. Control changes ship as their own
  `dev/workflow-qwen` PR (LOGBOOK, AGENTS, skill, controller scripts, .gitignore, config); worker-owned files
  never ride along.

## Verified baseline facts (run contract, unranked rehearsals only)

- Baseline-01 `run_f942e340ac385634`: 57.188 s, failed — every second model call HTTP 400 because replayed
  assistant messages dropped `tool_calls[].type`. Fixed by preserving `type:"function"`; proven by a real
  two-call hosted round-trip.
- Baseline-02 `run_afb43043d3561d5a`: 255 calls, 388.461 s (~6 m 28 s), failed with no answer. PROVEN: all four
  investigators hit the 60-step cap; the customer portal showed three identical repeated clicks; synthesis ran
  ≤14 steps, never called the calculator, produced no candidate, with ~823 s of budget left. INFERRED (code
  reading, not observed): any partial candidate was discarded. Repair brief lives in the runtime PR: per-tool
  tracing, repeated-action stall detection → forced replan/observe, wired deadline stop, candidate persistence
  (never fabricated facts, never an unconfirmed no_bid), focused second pass for missing facts.
- Public leaderboard: top quality 21.5, cost $0.3839, 984.9 s. Team `musedots` (team_8226e7213b542b20) is
  unranked: zero scored submissions attempted; the API `rehearsals` list holds the two failed unranked runs.
  A pasted 14:08 UTC dashboard screenshot predates both rehearsals (14:31, 14:38 UTC) and is stale.
- Origin `Jevalaggaan10/atira-tender-agent`: main carries only the initial setup. As of 17:4x origin has
  `dev/core-qwen` (PR #1 OPEN, verified) and no other dev branches; runtime/integration are still local WIP.
  Never claim pushed/verified work without an actual PR or run receipt.

## Timeline (UTC+2, this machine, 2026-10-10)

- ~15:30 Task + full official materials read; environment verified; interfaces contract published
  (`docs/INTERFACES.md`).
- ~15:40–16:57 First worker wave (then fast/no-thinking) produced runtime/core/integration code in the main
  tree; independent core audit + bounded hardening (duplicate ZIP members, overlapping undated validity
  windows rejected). Baselines 01/02 run and diagnosed (see facts above).
- ~16:57 Root created isolated worktrees + branch ownership (base `b828da6`); full suite at handoff
  179 passed / 11 failed (pricing doc line count, runtime fixture import-shape, finish-shape/kwargs-schema,
  synthetic browser fixtures) — assigned to owning PRs.
- ~17:0x Fresh-context master handoff (permissions fixed; prior master's 150k/65k config caused slow turns).
- ~17:09 Root bootstrapped the second worker wave in the three worktrees (max/thinking at the time),
  sessions recorded in `.dev-input/bootstrap-workers.json`.
- ~17:18 Human budget override: same three session ids RESUMED at worker budget (fast, thinking off, 8192;
  pids 64576 runtime / 64577 core / 64578 integration); master resumed medium/16384 (pid 64612). Config
  updated (variant timeouts 180/360, reviewer agent unprimed, no LOGBOOK seeding).
- ~17:2x–17:3x Controller built, root-reviewed; 10 findings fixed (git range args, log resolution, duplicate
  resume guard, launch identity, gate interpreter, foreign-file gate block, PR-cache TTL, dynamic origin truth,
  .gitignore `.venv`, unprimed reviewer). Controller status/watch/gate/review/resume/queue paths proven live
  against the real wave. Bounded next-action feedback queued for all three workers (delivered on session end —
  never injected into a live actor). Core guards editing; integration packaging guards landing; runtime flagged
  for a ~40-min no-edit stretch with a bounded repair list.
- ~17:4x CORE PR #1 OPEN on origin (`dev/core-qwen` @ b9e0d64, six owned files only): rule-faithful guards
  read and verified by master (zip name/index, windowed-value ambiguity per rule 21, rule 22/25 invariants);
  owned 133 passed independently reproduced; worker reported full 182/10 (10 = runtime PR's own files).
  A core-only GATE dry-run validated the pipeline (cherry-pick → 147 passed → ruff on changed files clean).
- ~17:5x Root terminated the idle old runtime session; fresh runtime context (new sid in bootstrap) edits in
  the same worktree — no duplicate actors (cap: 3 workers + master; reviewer shares the 4-actor budget).
  An independent reviewer session was started for PR #1; its `reviewer` agent mode was corrected to `all`
  after the CLI warned of a subagent→orchestrator fallback; no final verdict file exists yet — the review is
  still running and its findings must be verified before any forwarding.
- Manual unranked run `run_eadad1a2ecb4b357` (user-launched, OLD 16-file snapshot — does NOT contain current
  fixes): FINISHED FAILED, no answer, code-0 exit with ~275 s left — NOT a timeout. Pricing recovered after a
  setup-pools error and reached pricing_ok (a candidate existed; its decision must be inspected, not assumed
  bad). Both finalizer attempts burned 50 steps each: the same screen position was clicked repeatedly with
  the page unchanged, multiple TAN codes entered, zero receipts, zero observed validation handling. Bounded
  finalizer repair brief (page-signature stall detection, field-verified current-code entry, compact error
  evidence, honest receipt, generic synthetic tests) queued for the runtime worker. All coordinates/codes
  stay out of Git and prompts.
- Facts for the record: platform now allows 5 concurrent test runs (was 4) and exposes own-team field
  breakdown on scored public runs; Clef's typed `decide_typed` helper exists but is NOT called in runtime —
  Clef integration is explicitly incomplete (optional A/B routing after the baseline bid); the platform
  offers inference only — no tenant/LoRA/SFT/fine-tune API exists in the participant materials, so context
  engineering (budget tiers, LOGBOOK discipline, evidence state) is the whole lever. Entire: OpenCode hooks
  OK, git OK, 7 Codex hooks unapproved, 5 ended sessions uncondensed; never claim Entire fully clean; its
  refs stay local.

## Open risks / next gates

1. Runtime must still prove stall-repair + candidate persistence in tests; its PR review is the critical path.
2. Integration: entrypoint must connect all supplied CDP browsers, honor site-kind (never POST the answer API
   for site submissions); packaging allowlist + stage guards need guard-failing synthetic tests.
3. Independent final review stays unprimed; only demonstrated issues get forwarded; two failed repairs on one
   approach ⇒ change approach.
4. First objective proof: component PRs passing the combined gate, then an unranked sandbox measuring
   quality/cost/time. Nothing is "done" before a recorded receipt; no scored submission without the user's yes.
