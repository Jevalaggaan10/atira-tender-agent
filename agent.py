"""Your agent's entry point: agent.json runs `uv run --no-sync python agent.py`, in the sandbox and on your machine.

The kit does the plumbing (run.py reads the run and submits an answer, browsers.py attaches to the run's browsers,
models.py calls the hosted Qwen and Clef); the investigation lives in atira_agent/. Here we only build the run's
shared state: the browsers the sandbox gives us over its CDP endpoints (up to 4, never one of our own in a run), one
page per website so no two investigators share a page, the evidence store they share, and the hard clock: our own
monotonic cap (20 minutes for a scored run) and the server's `expires_at`, whichever comes first — enforced outside
the campaign so no phase can overrun it.

Fair-play: Qwen decides where to go, what to search, which documents to open and what the answer is. `solve` never
POSTs a site answer and never opens a browser of its own in a run.
Docs: $API/docs/building-an-agent.md, $API/docs/run-contract.md, and the rules: $API/docs/fair-play.md
"""
import asyncio
import json
import sys
import time
from urllib.error import URLError

from jsonschema import Draft202012Validator
from playwright.async_api import async_playwright

import browsers
import models  # noqa: F401  models.ask_qwen, models.ask_clef: see models.py
import run as contract
from atira_agent import events
from atira_agent.contracts import RunOutcome, RuntimeBudget
from atira_agent.evidence import EvidenceStore
from atira_agent.runtime import run_campaign

RUN_LIMIT_SECONDS = 1200.0  # a scored run is 20 minutes; we cap our own clock at that, wherever we run
RESERVE_SECONDS = 240.0  # kept aside for synthesis, the calculator and (for a site answer) signing and receipt
SUBMIT_FLOOR_SECONDS = 15.0  # the minimum the answer POST may still take
MAX_BROWSERS = 4


def make_budget(task: dict, *, limit_seconds: float = RUN_LIMIT_SECONDS,
                reserve_seconds: float = RESERVE_SECONDS) -> RuntimeBudget:
    """A monotonic local cap that also stops at the server's `expires_at`, whichever comes first."""
    expires_at = task.get("expires_at")
    expires_ms = float(expires_at) if isinstance(expires_at, (int, float)) and expires_at else None
    return RuntimeBudget(limit_seconds=limit_seconds, reserve_seconds=reserve_seconds, expires_at_ms=expires_ms)


def candidate_errors(candidate: dict, answer_schema: dict) -> list[str]:
    """What the run's own answer_schema rejects about the candidate ([] when it validates)."""
    if not isinstance(answer_schema, dict) or not answer_schema:
        return []
    try:
        validator = Draft202012Validator(answer_schema)
        return [f"{'/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"
                for error in sorted(validator.iter_errors(candidate), key=lambda e: list(e.absolute_path))][:20]
    except Exception as exc:  # a schema we cannot read is not a reason to send nothing
        return [f"answer_schema unreadable: {type(exc).__name__}"]


def _cdp_targets(env=None) -> int:
    """How many browsers the sandbox exports (0 locally, where browsers.connect launches its own)."""
    urls = browsers.cdp_urls() if env is None else browsers.cdp_urls(env)
    return len(urls)




async def _one_page(browser):
    """A page in a run's browser: the browser's own first page, or a tab of our own. Attach only — in a run the
    sandbox owns these browsers, so we never close what we did not open."""
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    return context.pages[0] if context.pages else await context.new_page()


async def _pages_for(playwright, count: int) -> tuple[list, list]:
    """Up to `count` pages, each in a browser of the run, as browsers.py's `connect` hands them to us.

    With fewer browsers than websites (agent.json "browsers": 4, four sites) this gives one page per browser and
    the campaign maps the remaining sites onto them; a page is never shared by two investigators at once."""
    opened: list = []
    pages: list = []
    for number in range(count):
        try:
            browser, page = await browsers.connect(playwright, number)
        except Exception as exc:  # a browser that refuses CDP is survivable: the rest carry on
            events.emit("browser_connect_failed", browser=number + 1, error=type(exc).__name__)
            break
        opened.append(browser)
        pages.append(page)
        events.emit("browser_ready", browser=number + 1)
    return opened, pages


async def solve(task: dict, page, pages: list | None = None,
                budget: RuntimeBudget | None = None) -> dict | None:
    """Run the model-directed campaign under a hard outer deadline, and say what actually happened.

    An "api" run returns the candidate for main() to POST once (validated against the run's answer_schema). A "site"
    run is entered on the website by the runtime finalizer, so we log the real outcome (receipt or reason) and return
    None: never a POST, never a success the site did not show.
    """
    all_pages = [page] + [p for p in (pages or []) if p is not page]
    store = EvidenceStore()
    budget = budget or make_budget(task)
    kind = (task.get("submission") or {}).get("kind") or "api"
    events.emit("start", run=task.get("run_id") or "local", sites=len(task.get("access") or []),
                pages=len(all_pages), kind=kind, budget_s=round(budget.remaining(), 1))
    try:
        outcome = await asyncio.wait_for(run_campaign(task, all_pages, store, budget),
                                         timeout=max(0.0, budget.remaining()))
    except TimeoutError:
        events.emit("deadline", remaining_s=0)
        print("Outer deadline reached: stopping the campaign.", flush=True)
        return _final_candidate(task, store, RunOutcome(submitted=False, reason="deadline reached"), budget)
    except Exception as exc:  # a crash mid-campaign still leaves the evidence and the clock for a best effort
        events.emit("campaign_error", error=type(exc).__name__)
        outcome = RunOutcome(submitted=False, reason=f"campaign error: {type(exc).__name__}")
    events.emit("campaign_done", submitted=bool(outcome.submitted), reason=(outcome.reason or "")[:120],
                evidence=store.stats().get("records"), remaining_s=round(budget.remaining(), 1))
    if kind == "site":
        if outcome.submitted:
            events.emit("site_submitted", receipt=bool(outcome.receipt))
            print(f"Bid entered on the website; receipt observed: {bool(outcome.receipt)}", flush=True)
        else:
            events.emit("site_not_submitted", reason=(outcome.reason or "")[:120])
            print(f"Bid NOT entered on the website: {outcome.reason or 'no reason given'}", flush=True)
            _log_partial(outcome.candidate)
        return None
    if outcome.submitted:
        print("Answer already accepted; nothing more to submit.", flush=True)
        return None
    return _final_candidate(task, store, outcome, budget)


def _final_candidate(task: dict, store, outcome: RunOutcome, budget: RuntimeBudget) -> dict | None:
    """The candidate to POST, if the run's own schema accepts it; a schema-invalid candidate is never sent."""
    candidate = outcome.candidate
    if candidate is None:
        print(f"No candidate to submit: {outcome.reason or 'no reason given'}", flush=True)
        return None
    errors = candidate_errors(candidate, task.get("answer_schema") or {})
    if errors:
        events.emit("candidate_rejected", errors=len(errors))
        print(f"Candidate does not fit the run's answer_schema ({len(errors)} issues), not submitting: "
              + "; ".join(errors[:5]), flush=True)
        _log_partial(candidate)
        return None
    events.emit("candidate_ready", remaining_s=round(budget.remaining(), 1))
    return candidate


def _log_partial(candidate: dict | None) -> None:
    """A bid we could not submit is still worth reading in the log: field names and counts, no page dumps."""
    if not isinstance(candidate, dict):
        return
    positions = candidate.get("positions")
    events.emit("partial_candidate", keys=sorted(candidate),
                positions=len(positions) if isinstance(positions, list) else None)


async def _close_one(browser, number: int) -> None:
    """Detach from a run's browser (or close a local one) without ever raising on the way out."""
    try:
        await asyncio.wait_for(browser.close(), timeout=5)
    except Exception as exc:  # a leftover browser must never mask the answer
        events.emit("browser_close_failed", browser=number, error=type(exc).__name__)


def _extra_pages(pages: list) -> list:
    """The pages beyond the first, filtered so that a plain `solve(task, page)` (the kit's shape) still works."""
    return [p for p in pages[1:] if p is not None and p is not pages[0]]


async def main() -> int:
    task, token = contract.load_run()
    sites = ", ".join(f"{site.get('siteId')} {site.get('url')}" for site in task["access"])
    print(f"Run {task.get('run_id') or '(local: the scouting logins, nothing counts)'}; websites: {sites}; "
          f"answer via {task['submission']['kind']}", flush=True)
    started = time.monotonic()
    try:
        return await _run(task, token, started)
    finally:
        events.emit("run_done", elapsed_s=round(time.monotonic() - started, 1))


async def _run(task: dict, token: str | None, started: float) -> int:
    """Attach to the run's browsers, solve under the clock, and post an api answer if there is one."""
    budget = make_budget(task)
    needed = max(1, len(task.get("access") or []))
    targets = _cdp_targets()
    if targets and needed > targets:
        print(f"{needed} websites but only {targets} browsers in this run: sites beyond the browser count share one, "
              f"so every site still gets a page but the campaign is slower.", flush=True)
    answer = None
    async with async_playwright() as playwright:
        opened, pages = await _pages_for(playwright, min(MAX_BROWSERS, needed))
        closing: list = []
        try:
            if not pages:
                print("No browser is reachable: nothing to investigate.", flush=True)
                return 1
            extra = _extra_pages(pages)
            if extra:
                answer = await solve(task, pages[0], extra, budget)
            else:  # one page (or a plain solve): the kit's two-argument shape
                answer = await solve(task, pages[0])
        finally:  # detach promptly and awaited: the run is over once the answer is out
            closing = [asyncio.ensure_future(_close_one(b, i + 1)) for i, b in enumerate(opened)]
            if closing:
                try:
                    await asyncio.wait_for(asyncio.gather(*closing, return_exceptions=True), timeout=5)
                except TimeoutError:
                    for pending in closing:
                        pending.cancel()
    if answer is None:
        print("No answer to submit (site kind, or nothing the schema accepts).", flush=True)
        return 0
    if not token:
        print("Your answer (locally nothing is submitted):", json.dumps(answer, ensure_ascii=False), flush=True)
        return 0
    # send whatever time is left: every part of an answer scores on its own, so a partial answer in the last
    # seconds still beats nothing — but never a POST the server would refuse after expires_at
    left = contract.seconds_left(task)
    if left <= 0:
        print("The run's time is up; the answer would be refused, so it is not sent.", flush=True)
        return 1
    status, body = 0, ""
    target = task.get("answer_url") or (task["submission"] or {}).get("url")
    try:
        status, body = await asyncio.to_thread(contract.http, target, token, answer,
                                               max(SUBMIT_FLOOR_SECONDS, min(60.0, left - 1.0)))
    except (URLError, OSError) as error:  # rides out a restart of the API, as run.py does for the read
        status, body = 0, str(error)
    print(f"Answer: HTTP {status} {str(body)[:2000]}", flush=True)
    events.emit("answer_submitted", status=status, elapsed_s=round(time.monotonic() - started, 1))
    return 0 if status == 202 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
