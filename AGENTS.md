# Atira development

Build a model-directed browser agent, not a scripted public-scenario solution.
Read the current run brief and use fresh runtime credentials, references and values.
Never copy scenario facts into source, prompts, tests, docs, defaults or fallbacks.
Tests use invented, clearly synthetic cases. Do not commit run evidence or transcripts.

Coding orchestration: parent plans, assigns bounded independent work to at most three
Atira-hosted Qwen workers, reviews diffs, integrates and validates.
Use explicit file ownership; workers must not change another worker's files or publish.
Parent alone creates releases and sends scored submissions after verification.
Use objective tests and recorded run results, never self-rated confidence as proof.

Worker isolation (user-authorized): each worker gets its own git worktree and branch from
.dev-input/worktree-plan.json, commits only its owned files, pushes only HEAD:dev/<name>-qwen,
opens a PR with gh, and writes a machine-readable worker-report.json. No direct main pushes, no
force-push, no merges by workers. The master reviews the actual diff and tests, runs the combined
gate on the union of owned files, then merges. Master alone updates LOGBOOK.md (single writer);
workers never edit it. Entire archives sessions locally; its refs stay local.

Model budgets (human override 2026-10-10, replaces the earlier max): parallel coding workers run the
lowest explicit budget (`opencode run --variant fast`, thinking disabled, output 8192); master and
reviewers run medium (thinking enabled, output 16384). These are explicit per-call budgets — do not
claim native Qwen reasoning_effort levels exist for them. Context 262144; no paid fallback.

Runtime: all model calls through provided Atira proxy; use supplied CDP browsers only.
Qwen chooses navigation/search/documents. Helpers parse selected content and calculate.
No humans, external services, local models, Entire or Git operations inside timed runs.
Keep publication code-only; Entire checkpoint refs must never be pushed to public origin.
