# Atira Tender Agent

Model-directed investigation across a tender portal, customer specification portal, ERP and mailbox, followed by a signed net-EUR bid. **Implementation in progress; no sandbox score claimed.**

The supplier portal contains the buyer's technical requirements; it is not a marketplace for buying components. The current challenge is a precise commercial reconciliation task, rather than a generic document-writing workflow.

## Development

Use Python 3.12 and [uv](https://docs.astral.sh/uv/). Install and sign in to the [official Atira CLI](https://ehl-challenge.atira.ai/start), then create or join a team. The captain links this repository on the team page.

```sh
uv sync
uv run playwright install chromium
# Separate terminal:
atira models proxy
# Back in this directory:
eval "$(atira scout --challenge rfq --env)"
uv run python agent.py
```

Scouting uses public, unscored logins. Sandboxed runs attach to the browsers supplied by Atira. The agent applies its own 20-minute limit, including longer rehearsal runs.

```sh
uv run pytest
uv run ruff check .
atira test-run . --challenge rfq --no-open
```

The development orchestrator delegates bounded tasks to up to three Atira-hosted Qwen workers and reviews their code and tests. It is separate from the timed Qwen agent. Entire captures development sessions locally; its checkpoint refs and transcripts must not be pushed to this public repository.

## Runtime design

Qwen chooses navigation, searches, documents and evidence reconciliation using the current run brief. Helpers parse selected documents, index page text, render selected scanned pages for hosted Qwen vision, and calculate exact decimal prices. Clef is an optional typed routing decision component; it cannot replace OCR or free-form requirement extraction. Calculations and submission gates remain deterministic.

Every run uses fresh credentials, references, revisions and commercial values. Scenario facts, credentials, downloaded records and transcripts are excluded from source and release packages. A shared evidence record is still required at runtime: Git and Entire do not replace the current bid's evidence, costs and submission state.

## Sources

- [Challenge and onboarding](https://ehl-challenge.atira.ai/start)
- [RFQ brief](https://ehl-challenge.atira.ai/challenges/rfq)
- [Run contract](https://ehl-challenge.atira.ai/docs/run-contract.md)
- [Agent development](https://ehl-challenge.atira.ai/docs/building-an-agent.md)
- [Fair play](https://ehl-challenge.atira.ai/docs/fair-play.md)
- [Scoring](https://ehl-challenge.atira.ai/docs/scoring.md)
- [Submissions](https://ehl-challenge.atira.ai/docs/submitting.md)
- [Announcements](https://ehl-challenge.atira.ai/announcements)
- [Public stage](https://ehl-challenge.atira.ai/challenges/rfq/stage)

The organizer recommends private repositories during the competition. This repository is public by its owner's explicit choice. It contains code and synthetic tests only.
