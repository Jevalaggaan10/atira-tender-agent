# Atira development

Build a model-directed browser agent, not a scripted public-scenario solution.
Read the current run brief and use fresh runtime credentials, references and values.
Never copy scenario facts into source, prompts, tests, docs, defaults or fallbacks.
Tests use invented, clearly synthetic cases. Do not commit run evidence or transcripts.

Coding orchestration: parent plans, assigns bounded independent work to at most three
Atira-hosted Qwen workers with high reasoning, reviews diffs, integrates and validates.
Use explicit file ownership; workers must not change another worker's files or publish.
Parent alone creates releases and sends scored submissions after verification.
Use objective tests and recorded run results, never self-rated confidence as proof.

Runtime: all model calls through provided Atira proxy; use supplied CDP browsers only.
Qwen chooses navigation/search/documents. Helpers parse selected content and calculate.
No humans, external services, local models, Entire or Git operations inside timed runs.
Keep publication code-only; Entire checkpoint refs must never be pushed to public origin.
