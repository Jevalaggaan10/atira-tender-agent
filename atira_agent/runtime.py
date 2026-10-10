"""run_campaign: brief pass -> parallel per-site investigators -> synthesis -> optional site submission.

Fair-play: every navigation target, search, document choice, field and decision is a fresh Qwen tool-call. This
module only starts agents, wires model-visible tools to helpers (browser reads, evidence store, document parsing,
the pure calculator), enforces the time budget and reports outcomes. Facts arrive only from the run
(`task["access"]`, `task["brief"]`) and from what the model reads at run time; nothing scenario-specific lives here.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import importlib
import inspect
import json
import re
import sys
import time
from typing import Any

from .contracts import SITE_ROLES, RunOutcome, RuntimeBudget
from .runtime_browser import SiteBrowser
from .runtime_models import ModelError, QwenAgent, Tool

# helpers wrap the model's single action / a model-selected document; a tool call never takes longer than this
TOOL_TIMEOUT = {"browser": 45.0, "document": 20.0, "evidence": 5.0}
INVESTIGATOR_STOP_SLACK = 120.0  # stop investigating this early; the reserve is for synthesis + submission
FINALIZER_SLACK = 30.0  # keep this much aside for the finalizer (site kind) — no phase may eat the reserve
# measured from a real run: all four investigators burned the whole phase in step-capped loops and synthesis
# never reached the calculator with 823 s still free. Each phase is now also capped on its own share of the
# budget, and repeated no-change actions steer the model back to observing instead of clicking the same thing.
INVESTIGATOR_MAX_SECONDS = 420.0  # one wave slice per investigator, budget-capped below
SYNTHESIS_MIN_SLICE = 150.0  # synthesis must keep a usable slice even when investigators were slow
NO_PROGRESS_REOBSERVE_AFTER = 3  # this many ineffective repeats of one action before a forced re-observe
NO_PROGRESS_REPLAN_AFTER = 8  # rounds without meaningful progress -> forced replan note
MAX_RECOVERY_ROUNDS = 5  # rounds a stuck agent gets to recover...
MAX_RECOVERY_STRIKES = 2  # ...per strategy; after this many failed recoveries the site is blocked (evidence kept)
FOCUS_WAVE_STEPS = 20  # a focused follow-up wave: model-named missing facts, bounded rounds per site


def _emit(event: str, **fields: Any) -> None:
    """Log through the parent's events module when present; field values must carry no secrets or payloads.

    Looked up by name first: a stand-in installed in sys.modules by a test must be visible, and a stale
    `from . import events` binding (when the real module was imported earlier) would hide it."""
    try:
        events = sys.modules.get("atira_agent.events")
        if events is None:
            events = importlib.import_module(".events", __package__)  # parent-owned; loaded at call time
    except Exception:  # pragma: no cover
        return
    try:
        events.emit(event, **fields)
    except Exception:  # pragma: no cover
        pass


def _page_fingerprint(observed: Any) -> str:
    """A short, stable hash of WHAT the model currently sees (url, title, element count, text length).

    Diagnostic and loop-detection only: the value itself goes nowhere — it carries no content, no payload and
    no secret, just enough to notice that the same action left the page unchanged."""
    if not isinstance(observed, dict):
        return ""
    try:
        digest = hashlib.sha1(
            f"{observed.get('url', '')}|{observed.get('title', '')}|{len(observed.get('elements') or [])}"
            f"|{len(str(observed.get('text', '') or ''))}".encode()).hexdigest()[:12]
    except Exception:  # pragma: no cover - a duck-typed observation may be sparse
        return ""
    return digest


def _observed_shape(observed: Any) -> dict:
    """Shape counts of an observation for a diagnostic event: counts only, never the content."""
    if not isinstance(observed, dict):
        return {}
    try:
        hints = observed.get("hints") or {}
        return {"url": str(observed.get("url", ""))[:120], "elements": len(observed.get("elements") or []),
                "text_len": len(str(observed.get("text", "") or "")),
                "popup": bool(hints.get("popup_hint")), "login": bool(hints.get("login_hint"))}
    except Exception:  # pragma: no cover
        return {}


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return json.loads(json.dumps(value, default=str))


def _dump(value: Any, cap: int = 12_000) -> str:
    text = json.dumps(_jsonable(value), ensure_ascii=False)
    return text if len(text) <= cap else text[:cap] + "…[truncated]"


def _image_part(data: bytes) -> dict:
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(data).decode()}}


def _tool(fn, description: str, props: dict, limit: float) -> Tool:
    """Expose a helper as a model tool with a per-tool timeout (browser/document/evidence scales differ).

    The tool's `fn` is a wrapper whose signature is the HELPER'S OWN signature (positional-or-keyword, keyword-
    only and *args/**kwargs carried over exactly). Every schema key is therefore an accepted keyword: the
    signature a test or a caller inspects matches what the call accepts, and a missing or extra argument
    TypeErrors at once instead of drifting through a **kwargs catch-all. The wrapper forwards the model's
    kwargs to the helper and _dispatch runs it: coroutine helpers bounded, sync helpers (documents.*, pricing)
    on a worker thread, and any awaitable a sync lambda returns awaited with the time remaining."""

    def wrapper(**kwargs):
        return _dispatch(fn, limit, **kwargs)  # a sync call returning the (awaiting) dispatcher coroutine

    try:  # publish the helper's real signature: schema keys are accepted keywords, mismatches TypeError at once
        wrapper.__signature__ = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover - a built-in without a signature keeps **kwargs
        wrapper.__signature__ = inspect.signature(lambda **kwargs: None)
    wrapper.__doc__ = getattr(fn, "__doc__", None)
    tool = Tool(description, {"type": "object", "properties": props}, wrapper)
    tool.timeout = limit
    return tool


def _tool_kwargs(fn, schema_keys) -> dict:
    """The helper's own keyword parameters, plus every schema key the model may send.

    A schema key missing from the helper's parameters is appended as an OPTIONAL keyword so the call still
    matches; the helper receives it and decides what to do with an extra keyword (TypeError -> the loop's
    tool-error note). VAR_KEYWORD (**kwargs) helpers accept whatever the model chooses."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover - a built-in without a signature
        return dict.fromkeys(schema_keys, inspect.Parameter.empty)
    accepted = {p.name for p in sig.parameters.values() if p.kind in
                (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY, p.VAR_KEYWORD)}
    return dict.fromkeys(sorted(set(accepted) | set(schema_keys)), inspect.Parameter.empty)


async def _dispatch(fn, limit: float, /, *args, **kwargs):
    """One dispatcher for every helper: coroutine functions run bounded; ordinary functions (documents.*,
    pricing, plain lambdas) run on a worker thread so the event loop and the other investigators keep moving.
    A sync callable that RETURNS an awaitable (e.g. a lambda calling an async method) is not left in a thread
    that dies at the slice: the awaitable is carried back to the loop and awaited with the time remaining, so
    the helper completes (or the slice ends) and the model never sees a bare coroutine object. The helper's own
    exception (PricingError, DocumentError, ...) passes through — callers that turn failures into
    model-visible notes do so themselves."""
    limit = max(0.5, limit)
    started = time.monotonic()
    is_future = asyncio.isfuture  # resolved per call: a test may patch the asyncio module's surface
    if inspect.iscoroutinefunction(fn):
        return await asyncio.wait_for(fn(*args, **kwargs), timeout=limit)
    if is_future(fn):  # an already-running future: join it, never thread it
        return await asyncio.wait_for(fn, timeout=limit)
    outcome = await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=limit)
    if asyncio.iscoroutine(outcome) or is_future(outcome):
        # the helper returned an awaitable: complete it on this loop bounded by the slice that is LEFT after
        # the sync part ran, not by a fresh full limit; on timeout never abandon it — cancel and reap it
        remaining = max(0.1, limit - (time.monotonic() - started))
        try:
            return await asyncio.wait_for(outcome, timeout=remaining)
        except asyncio.TimeoutError:
            outcome.cancel() if is_future(outcome) else outcome.close()
            with contextlib.suppress(BaseException):  # drain the cancellation so nothing lingers unretrieved
                await outcome
            raise
    return outcome


async def _call(label: str, limit: float, budget: RuntimeBudget, fn, /, *args, **kwargs):
    """Run one helper bounded: the tool limit, but never more than the budget still allows; sync off-loop."""
    try:
        return await _dispatch(fn, min(limit, budget.timeout(limit)), *args, **kwargs)
    except asyncio.TimeoutError:
        return {"error": f"{label} timed out; the page or document may be slow — try again or move on"}
    except Exception as exc:
        return {"error": f"{label} failed: {type(exc).__name__}: {exc}"}


def _finish_handler(param: str, state: dict | None = None):
    """The terminal `finish` tool for one agent contract (`finish(summary_json)` / `finish(result_json)`).

    The loop's terminal result is normally the decoded `result_json` argument; a CUSTOM finish tool (this one)
    keeps its own returned value, so it must honor the same contract:

    * the declared argument present and empty -> {"error"}: the model must re-call with a payload;
    * present but not decodable JSON -> {"error"} — never a silent passthrough of free text;
    * decodable dict -> the decoded payload; any other JSON shape -> {"summary": ...};
    * no argument at all -> the loop's own (already decoded) result back, unless the tool was pre-seeded with
      a completed calculator answer in `state["final"]`, which the loop then preserves.
    """

    async def finish_handler(**kwargs) -> Any:
        if param not in kwargs or kwargs.get(param) in (None, ""):
            if state is not None and isinstance(state.get("final"), dict):
                return dict(state["final"])  # the loop echoes this result as its terminal answer
            return {"error": f"finish needs {param}: pass the JSON string your contract describes"}
        raw = kwargs[param]
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except ValueError:
                return {"error": f"{param} is not valid JSON: pass the JSON string your contract describes"}
        else:
            parsed = raw
        if isinstance(parsed, dict):
            return parsed
        return {"summary": str(parsed)}

    return finish_handler


def _site_role(site_id: str, roles: dict[str, str] | None) -> str:
    if roles and site_id in roles:
        return roles[site_id]
    return SITE_ROLES.get(site_id, site_id)


def _schema_hint(answer_schema: dict, limit: int = 2500) -> str:
    try:
        return _dump(answer_schema, cap=limit)
    except Exception:
        return str(answer_schema)[:limit]


def _brief_system(sites: list[dict], answer_schema: dict) -> str:
    return (
        "You are the planner of a tender-bid agent system. You will not touch any website yourself. From the run "
        "brief below, write a short list of investigation missions per website role: what that site is likely to "
        "hold for this tender (based on the brief's own words), in generic terms. Missions are goals, never click "
        "paths and never invented facts — a mission may only name things the brief itself states. "
        f"Websites: {_dump([{'siteId': s['site_id'], 'role': s['role'], 'url': s['url']} for s in sites])}. "
        f"The answer the campaign must produce has this schema: {_schema_hint(answer_schema)}. "
        'Finish with result_json = {"missions": {"<siteId>": ["<goal>", ...], ...}}.'
    )


def _investigator_system(site: dict, all_sites: list[dict], missions: list[str], brief: str,
                         answer_schema: dict, schema_keys: list[str]) -> str:
    return (
        "You are the investigator for exactly one website of a tender campaign. The site's own login, when a page "
        "asks for it, comes from the credentials() tool — enter it when and where the page demands it. The "
        "credentials of the other websites are available via site_credentials(site_id) when the brief tells you "
        "one site references another site's data; you never visit another site, only investigate this one.\n"
        "The run brief is authoritative; work out what it needs from THIS website. Missions for this site: "
        f"{missions}.\n"
        "Method: goto the site, read the observation (visible text + indexed elements) and screenshot() when the "
        "text is not enough, then choose your next click, search or link yourself. Pages change asynchronously: "
        "re-observe after every action. Popups and cookie walls block the view: act('accept_cookies') or "
        "dismiss-style buttons you observe. Search rather than crawl: run searches the brief or the page suggests, "
        "and narrow any over-broad result set (refine the query) to at most 20 results before opening documents. "
        "Downloads store in evidence (kind=document) and return a rid; read them with pdf_texts/zip_list/"
        "zip_read_text, or pdf_render(page) to look at a page as an image. Older revisions, look-alike records and "
        "superseded notices exist: record revision and date with every fact, check for conflicts with "
        "evidence_conflicts() and resolve them by supersede/invalidate with reasons.\n"
        "Record every durable fact into evidence with evidence_add(kind, ref, title, revision, dated, authority, "
        "text): quantities, clarifications, specification revisions, materials, price conditions and validity "
        "windows, stock, discounts and approvals, surcharge/packaging/bonus/index rules, deadlines, scenario date, "
        "configurator rules, signing steps. Quote exact numbers and references in the record text. Other "
        "investigators share this evidence store: record, do not wait for others.\n"
        f"The campaign's final answer schema: {_schema_hint(answer_schema)}.\n"
        "The brief is finite: work through your missions as a checklist and finish as soon as each need from "
        "this site is recorded or you genuinely cannot resolve it here — do not browse past the checklist. A "
        "second wave and the synthesis agent build on what you record, so record durable facts rather than "
        "re-reading the same page. "
        "Stop and finish when the brief's needs from this site are recorded or you are truly stuck. "
        'finish(summary_json) = {"summary": "<what you found and left open>", "records": ["rid", ...]}.\n'
        f"RUN BRIEF (authoritative):\n{brief}"
    )


def _synthesis_system(brief: str, schema_doc: str, answer_schema: dict, stats: dict, conflicts: list,
                      snapshot: str = "") -> str:
    return (
        "You are the pricing and answer synthesis agent of a tender campaign. Investigators have recorded what the "
        "websites hold into a shared evidence store; your job: assemble the pricing payload for this campaign's "
        "calculator, run it, and finish with the answer.\n"
        f"Tool pricing_calculate(payload) is the campaign's calculator; payload = the calculator's JSON as a "
        f"string. Payload schema (with a synthetic example):\n{schema_doc}\n"
        f"The run's answer schema:\n{_schema_hint(answer_schema)}.\n"
        f"Evidence: stats {_dump(stats)}; conflicts {_dump(conflicts)}.\n"
        f"ACTIVE EVIDENCE SNAPSHOT (inline, bounded; read it FIRST, it is already in front of you):\n{snapshot}\n"
        "Read the brief's rules and the evidence snapshot yourself; the evidence tools are capped (3 calls each "
        "across evidence_snapshot, evidence_stats and evidence_conflicts combined), so use the inline snapshot "
        "first and spend tool calls only on what it leaves open; use evidence_snapshot(kind?,ref?,contains?) and "
        "evidence_stats() to decide what matters and evidence_conflicts() where revisions clash. Apply the brief's "
    "rules to the payload fields yourself; every amount as a decimal string; the scenario date from the brief. "
    "If pricing_calculate answers {\"error\", \"path\"}, fix the payload at that path and call it again "
    "(at most 3 fixes; after that finish with the best you have). On success, finish(result_json) with the "
    "calculator's returned `answer` as a JSON string; the run adds notes itself. Work efficiently: do not re-read "
    "the same evidence over and over — once the facts you need are in front of you, build the payload and call "
    "the calculator; it is the tool that produces the answer, so call it EARLY using the inline evidence "
    "snapshot first, and refine the payload from its errors; once pricing_calculate succeeds, finish immediately "
    "with its answer. If you cannot price, finish with your best partial answer candidate per the answer schema. "
    "A no-bid is the semantic decision you own: only ever when an ACTIVE record shows no exact technical "
    "match is possible (a missing material in the payload also forces it). Missing research is never a "
    "no-bid — say what you could not find instead. When you are sure, finish with "
    "{\"decision\":\"no_bid\",\"tender_reference\":...,\"supporting_refs\":[<rid ids you actually read>],"
    "\"reason\":...}; the run verifies those ids are real active records before accepting the no-bid.\n"
        f"RUN BRIEF (authoritative):\n{brief}"
    )


def _validate_candidate(candidate: dict, answer_schema: dict) -> tuple[bool, list[str]]:
    """Validate a candidate against THIS run's answer schema (keys/types/declared enums), not scenario facts.

    Returns (ok, problems). An intentional omission (brief rules: "Position auslassen", partial credit) is a
    legitimate shape: the schema — not a fixed rule — decides what a position needs, so the gate checks the
    keys and types the schema declares and never requires full position coverage."""
    problems: list[str] = []
    if not isinstance(candidate, dict):
        return False, ["candidate is not an object"]
    props = answer_schema.get("properties") if isinstance(answer_schema, dict) else None
    if not isinstance(props, dict) or not props:
        return True, []  # no schema to validate against: a calculator-produced answer stands as-is
    for key in (answer_schema.get("required") or []):
        if key not in candidate:
            problems.append(f"required field {key!r} missing")
    for key, spec in props.items():
        if key not in candidate or not isinstance(spec, dict):
            continue
        value = candidate[key]
        expected = spec.get("type")
        simple = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "object": dict,
                  "array": list}
        if expected in simple and not isinstance(value, simple[expected]):
            problems.append(f"field {key!r} must be {expected}")
        elif expected == "number" and isinstance(value, bool):
            problems.append(f"field {key!r} must be a number")
        allowed = spec.get("enum")
        if isinstance(allowed, list) and allowed and value not in allowed:
            problems.append(f"field {key!r} value not among the schema's choices")
        if expected == "array" and isinstance(value, list) and isinstance(spec.get("items"), dict):
            item_spec = spec["items"]
            item_props = item_spec.get("properties") if isinstance(item_spec, dict) else None
            if isinstance(item_spec, dict) and item_spec.get("type") == "object" and isinstance(item_props, dict):
                for position_no, item in enumerate(value):
                    if not isinstance(item, dict):
                        problems.append(f"{key}[{position_no}] must be an object")
                        continue
                    for field in (item_spec.get("required") or []):
                        if field not in item:
                            problems.append(f"{key}[{position_no}] missing {field!r}")
                    for field, field_spec in item_props.items():
                        if field in item and isinstance(field_spec, dict) and field_spec.get("type") in simple \
                                and not isinstance(item[field], simple[field_spec["type"]]):
                            problems.append(f"{key}[{position_no}].{field} must be {field_spec['type']}")
    # a bid must carry at least one priced position; an empty positions list is only honest with the brief's
    # own no-bid vocabulary — a missing fact is never silently a no-bid
    if isinstance(candidate.get("decision"), str) and candidate["decision"].lower().startswith("bid"):
        positions = candidate.get("positions")
        if not (isinstance(positions, list) and positions):
            problems.append("decision is a bid but positions carry no priced line")
    return not problems, problems


def _finalizer_system(brief: str, candidate: dict, answer_schema: dict, attempt: int = 1,
                      validation: tuple[bool, list[str]] | None = None) -> str:
    extra = ("This is your SECOND attempt: the portal rejected the previous sign. Re-read a FRESH signing TAN "
             "from the mail site before signing again, and never reuse the old TAN.\n" if attempt == 2 else "")
    note = ""
    if validation is not None and not validation[0]:
        note = ("Validation notes (this run's answer schema): " + "; ".join(validation[1])[:500] + "\n"
                "Fill what the candidate carries; the brief allows omitting unfinished positions for partial "
                "credit — a signed partial with a real receipt beats an unfinished perfect form.\n")
    return (
        "You are the submission finalizer of a tender campaign. The prepared bid answer is below. Enter it into "
        "the tender portal's bid configurator using the portal_* tools: open the portal (portal_goto starts at "
        "this run's tender portal), find the bid form for this tender yourself, fill each field (portal_act fill "
        "with the element index you observed), and re-observe to verify each value landed. Omitted positions: "
        "the form's own partial rules apply (brief: \"Position auslassen\" / partial credit): leave their "
        "fields empty or use the form's explicit omit option if it has one — never invent values for an "
        "omitted position.\n"
        + note + extra +
        "Signing comes LAST: the signing TAN is short-lived — at the signing moment, use mail_goto/mail_observe/"
        "mail_act to find the fresh signing-TAN mail on the mail site, read the code, enter it and confirm on "
        "the portal. Never store the code in evidence; never reuse an old code. A rejection is NOT always an "
        "expired code: read the page's own validation/error lines and fix what they name (a wrong or missing "
        "field, an unchanged value). Only assume expiry when the page says so, and then request a FRESH code "
        "from the mail site — never resubmit the same code blind. Re-observe the page after each sign attempt: "
        "a receipt may land late (a confirmation page counts once it shows), and never submit a second time "
        "over a submission that already landed. If that fails again, stop and finish with submitted=false and "
        "what you saw. Never post the answer to any API — the site's own form is the submission.\n"
        f'finish(result_json) = {{"submitted": true|false, "receipt": "<receipt text as observed>", '
        '"reason": "<why not, when false>"}.\n'
        f"Prepared answer:\n{_dump(candidate, cap=20_000)}\nAnswer schema: {_schema_hint(answer_schema)}\n"
        f"RUN BRIEF (authoritative):\n{brief}"
    )


async def run_campaign(task: dict, pages: list, store, budget: RuntimeBudget,
                       roles: dict[str, str] | None = None, *, pricing_module=None) -> RunOutcome:
    """Model-directed campaign over the run's websites; returns the outcome (parent handles api posting/receipts).

    `pricing_module` pins the calculator explicitly (a test injects its invented stub without touching
    sys.modules); production leaves it None and the real module resolves at call time."""
    access = [entry for entry in (task.get("access") or []) if isinstance(entry, dict) and entry.get("siteId")]
    if not access:
        return RunOutcome(submitted=False, reason="no websites in this run's access")
    if not pages:
        return RunOutcome(submitted=False, reason="no browser pages supplied")
    brief = str(task.get("brief") or "")
    answer_schema = task.get("answer_schema") or {}
    kind = (task.get("submission") or {}).get("kind") or "api"
    # the run contract's monotonic wall, EXCEPT where a caller proves a shorter experiment: a rehearsal grant
    # (2 h test runs) does not stretch a campaign across hours, and the reserve (at least 240 s) is what
    # synthesis + site signing keep — but a budget already shorter than the cap is the caller's own limit
    budget.limit_seconds = min(float(budget.limit_seconds), 1200.0)
    if budget.limit_seconds >= 1200.0:  # a production-scale run gets the full signing reserve; tests pin less
        budget.reserve_seconds = max(float(budget.reserve_seconds), 240.0)
    _BUDGET_HOLDER["get"] = lambda: budget  # helpers (decision review) resolve the campaign's shared budget

    sites: list[dict] = []
    for position, entry in enumerate(access):
        site_id = str(entry["siteId"])
        browser = SiteBrowser(pages[position % len(pages)], site_id, budget=budget)
        browser.attach_credentials(dict(entry.get("credentials") or {}))
        sites.append({"site_id": site_id, "url": entry.get("url") or "", "role": _site_role(site_id, roles),
                      "entry": entry, "browser": browser})
    by_id = {site["site_id"]: site for site in sites}

    # the campaign's shared decision state (internal, never portal-visible): what the calculator's answer was
    # and why (kind/error_path/evidence_refs), so the finalizer can judge a no-bid against REAL evidence
    campaign: dict = {"summaries": [], "decision": {}, "answer_schema": answer_schema}
    _emit("phase", phase="brief")
    missions = await _brief_pass(brief, sites, answer_schema, budget)
    _emit("phase", phase="investigate", sites=[s["site_id"] for s in sites])
    summaries = await _investigate_all(sites, missions, brief, answer_schema, store, budget)
    campaign["summaries"] = summaries
    # measured failure mode: the last investigator finished so late that no calculator call ever ran although
    # hundreds of seconds were still free. Synthesize when investigators finish early, or when the budget says
    # now — the phase caps below keep both directions bounded.
    wave_two = _missing_fact_needs(second_round_targets(summaries, budget))
    if wave_two:
        _emit("second_wave", sites=sorted({t["site_id"] for t in wave_two}))
        await _investigate_second_wave(sites, wave_two, brief, answer_schema, store, budget)
    _emit("phase", phase="synthesize")
    candidate = await _synthesize(brief, answer_schema, store, budget, kind, pricing_module=pricing_module,
                                  campaign=campaign)
    if isinstance(campaign, dict) and isinstance(candidate, dict):
        # the calculator's provenance (kind, no-bid path, attested refs) rides into the campaign's shared
        # decision state; it is internal and never part of the portal answer
        campaign["decision"] = dict(candidate.get("provenance") or {})
    answer = candidate.get("answer") if isinstance(candidate, dict) else candidate
    answer = _strip_meta_fields(answer)
    if not answer:  # None or an empty object: honestly nothing to submit
        _emit("phase", phase="done", outcome="no candidate")
        return RunOutcome(submitted=False, reason="synthesis produced no candidate")
    candidate = answer
    if kind != "site":
        _emit("phase", phase="done", outcome="api candidate ready")
        return RunOutcome(submitted=False, candidate=candidate, reason="api")
    # candidate_validated (the run's contract): the candidate must fit THIS run's answer schema before any
    # signing time is spent — omissions per the brief stay valid; the problems guide, they never fabricate
    ok, problems = _validate_candidate(candidate, answer_schema)
    _emit("candidate_validated", ok=ok, problems=problems[:6])
    _emit("phase", phase="finalize")
    outcome = await _finalize_site(brief, answer_schema, candidate, sites, by_id, store, budget,
                                   validation=(ok, problems))
    _emit("phase", phase="done", submitted=outcome.submitted)
    return outcome


# ---- brief pass -----------------------------------------------------------------------------------------------


async def _brief_pass(brief: str, sites: list[dict], answer_schema: dict, budget: RuntimeBudget) -> dict:
    agent = QwenAgent("brief", _brief_system(sites, answer_schema), {}, budget=budget)
    try:
        result = await _await_bounded(agent.run(f"Write the per-site missions.\nRUN BRIEF:\n{brief}",
                                                max_steps=6), budget, 120.0)
    except ModelError as exc:  # a missing planner is survivable: investigators still get the brief itself
        _emit("model_error", agent="brief", error=type(exc).__name__)
        return {}
    missions = result.get("missions") if isinstance(result, dict) else None
    return {str(k): list(v) for k, v in missions.items() if isinstance(v, list)} if isinstance(missions, dict) else {}


async def _await_bounded(coro, budget: RuntimeBudget, limit: float):
    return await asyncio.wait_for(coro, timeout=max(1.0, min(limit, budget.remaining())))


# ---- investigators -------------------------------------------------------------------------------------------


async def _investigate_all(sites: list[dict], missions: dict, brief: str, answer_schema: dict, store,
                           budget: RuntimeBudget) -> list[dict]:
    if budget.should_finalize():
        _emit("investigators_skipped", reason="budget at/below reserve")
        return []
    # the hard gate: what is left minus the reserve and the investigator stop slack. A model that ignores the
    # tool's stop note is cancelled at the deadline, so it cannot spend the reserve on further model calls.
    slice_s = min(budget.remaining() - budget.reserve_seconds - INVESTIGATOR_STOP_SLACK, 600.0 - (time.monotonic() - budget.started))
    if slice_s < 0.5:
        _emit("investigators_skipped", reason="no slice above the reserve")
        return []
    # measured failure mode: investigators looped step-capped for the entire phase and synthesis was squeezed
    # out with plenty of budget free. Cap one investigator's effort per wave so the phase cannot eat the run.
    # At most TWO sites per wave keep the concurrent model calls inside the platform's allowance.
    slice_s = min(slice_s, max(60.0, INVESTIGATOR_MAX_SECONDS))

    async def bounded(site):
        try:
            return await asyncio.wait_for(_investigate(site, missions.get(site["site_id"], []), brief,
                                                       answer_schema, sites, store, budget), timeout=slice_s)
        except asyncio.TimeoutError:
            return {"summary": "hard deadline reached: the site kept working past its slice; evidence stands",
                    "status": "phase_timeout", "records": [], "stopped": "deadline",
                    "blocker": "phase slice expired before the investigator finished"}

    answers = await asyncio.gather(*(bounded(site) for site in sites), return_exceptions=True)
    summaries = []
    for site, answer in zip(sites, answers):
        if isinstance(answer, BaseException):
            _emit("investigator_error", site=site["site_id"], error=type(answer).__name__)
            summaries.append({"site": site["site_id"], "summary": None, "status": "error",
                              "unresolved": [], "blocker": type(answer).__name__,
                              "error": type(answer).__name__})
        else:
            row = {"site": site["site_id"], **{k: answer.get(k) for k in
                                               ("summary", "status", "unresolved", "blocker",
                                                "records", "stopped") if k in answer}}
            row["record_count"] = len(answer.get("records") or []) if isinstance(answer.get("records"),
                                                                                 list) else 0
            summaries.append(row)
    _emit("investigated", summaries=_dump(summaries, cap=4000))
    return summaries


# Generic, language-neutral vocabulary for what a pricing payload needs (categories, not facts). The model
# still decides what to search and read; these only word the second-wave focus and the model's openNeeds.
PRICE_FIELD_WORDS = ("menge", "quantity", "preis", "price", "material", "katalog", "catalog", "bestand",
                     "stock", "rabatt", "discount", "datum", "date", "revision")


def second_round_targets(summaries: list[dict], budget: RuntimeBudget) -> list[dict]:
    """Focused follow-ups the first wave's EXPLICIT statuses motivate, while budget remains.

    A site whose worker reported unresolved needs or a non-complete status (blocked, step_limit,
    phase_timeout, error) gets one focused second wave — the worker's own words name the gaps, the model
    picks the facts. A site that finished complete is not second-guessed from its record count: that would
    be a fixed rule about what the model should have found. Never more than two sites, never once the
    reserve is at hand.
    """
    if budget.should_finalize() or budget.remaining() <= budget.reserve_seconds + INVESTIGATOR_STOP_SLACK + 60.0:
        return []
    targets = []
    for row in summaries:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "")
        unresolved = row.get("unresolved") if isinstance(row.get("unresolved"), list) else []
        # only EXPLICIT nonempty unresolved factual needs motivate a second wave; a non-complete status alone
        # (blocked, step_limit, phase_timeout, error) is preserved for reporting but never triggers a broad
        # restart of the site without the worker naming what is still missing
        needs = [item for item in unresolved if str(item).strip()]
        if not needs:
            continue
        focus = "; ".join(str(item)[:80] for item in needs[:4]) or str(row.get("blocker") or "")[:120]
        if focus:
            targets.append({"site_id": row.get("site"), "focus": focus})
    return targets[:2]


def _missing_fact_needs(targets: list[dict]) -> list[dict]:
    """Keep only follow-ups a genuine unresolved need or non-complete status motivates (see above)."""
    return [t for t in targets if t.get("focus")]


async def _investigate_second_wave(sites: list[dict], targets: list[dict], brief: str, answer_schema: dict,
                                   store, budget: RuntimeBudget) -> None:
    by_id = {site["site_id"]: site for site in sites}
    # the wave stays bounded: at most the two most-motivated sites, so the concurrent model calls (plus any
    # other agent) stay within the platform's five-call test allowance and four for scored runs
    wanted = [by_id[t["site_id"]] for t in targets if t.get("site_id") in by_id][:2]
    if not wanted:
        return
    # the exact selected focus of each target rides into its site's second wave
    focus_by_site = {t["site_id"]: str(t.get("focus") or "") for t in targets}
    slice_s = min(150.0, min(600.0 - (time.monotonic() - budget.started), budget.remaining() - budget.reserve_seconds - INVESTIGATOR_STOP_SLACK))
    if slice_s < 20.0:
        _emit("second_wave_skipped", reason="slice too small above the reserve")
        return

    async def bounded(site):
        selected = focus_by_site.get(site["site_id"], "")
        focus = ("This is a FOCUSED second wave for this site. The shared evidence shows gaps: read "
                 "evidence_stats() and evidence_snapshot() first, work out which facts THIS site likely holds "
                 "and are still missing or thin (the pricing payload needs them: quantities, prices, materials "
                 "and catalog groups, revisions and dates, stock, discount rules), then look for exactly those "
                 "facts — search rather than browse, record each with evidence_add, and finish quickly. Do not "
                 "re-read what is already recorded.")
        if selected:
            focus = f"{focus}\nSelected focus for this site (the first wave's own unresolved needs): {selected}"
        try:
            total = docs = 0
            for row in store.snapshot(limit=1000):
                total += 1
                if row.get("kind") == "document":
                    docs += 1
            counts = f"total={total}, documents={docs}"
        except Exception:  # pragma: no cover - a duck-typed store may be minimal
            counts = "unknown"
        focus = f"{focus}\nEvidence counts now: {counts}."
        try:
            return await asyncio.wait_for(_investigate(site, [], brief, answer_schema, sites, store, budget,
                                                       focus=focus), timeout=slice_s)
        except asyncio.TimeoutError:
            return None

    await asyncio.gather(*(bounded(site) for site in wanted), return_exceptions=True)


async def _investigate(site: dict, missions: list[str], brief: str, answer_schema: dict, all_sites: list[dict],
                       store, budget: RuntimeBudget, focus: str | None = None) -> dict:
    site_id, browser = site["site_id"], site["browser"]
    entry = site["entry"]
    schema_keys = sorted((answer_schema.get("properties") or {}).keys()) if isinstance(answer_schema, dict) else []
    deadline = budget.reserve_seconds + INVESTIGATOR_STOP_SLACK

    def stop_check_now() -> dict | None:
        if budget.remaining() <= deadline:
            return {"summary": "stopped early: the budget reserve was reached; what is in evidence stands",
                    "records": [], "stopped": "budget"}
        return None

    async def goto(url: str) -> dict:
        result = await _call("goto", TOOL_TIMEOUT["browser"], budget, browser.goto, url)
        return track(result, "goto", url=url)

    async def observe() -> dict:
        result = await _call("observe", TOOL_TIMEOUT["browser"], budget, browser.observe)
        return track(result, "observe")

    async def act(action: str, index: int | None = None, value: str | None = None,
                  selector: str | None = None) -> dict:
        action = (action or "").lower()
        key = json.dumps({"action": action, "index": index, "selector": selector, "value": value},
                         sort_keys=True)[:120]
        if (action in {"click", "fill", "press", "select"} and loop["progress_streak"] >= NO_PROGRESS_REPLAN_AFTER
                and loop["recoveries"] > 0 and key == loop["last_key"]):
            # the model tried the SAME stuck action again even after the replan note: refuse it, cheaply
            return {"actioned": False, "changed": False,
                    "note": "Refused: you repeated this unchanged strategy after the replan note. observe(), "
                            "screenshot(), then a genuinely different route — or finish."}
        result = await _call("act", TOOL_TIMEOUT["browser"], budget, browser.act, action, index, selector, value)
        fingerprint = _page_fingerprint(result) if isinstance(result, dict) else ""
        if action in {"click", "fill", "press", "select"} and page_state["fingerprint"] is not None:
            # the page after this action vs the page when the same action last ran: unchanged -> force a change
            if key == loop["last_key"] and fingerprint and fingerprint == loop["last_fp"]:
                page_state["fingerprint"] = fingerprint  # keep the tracker current even on a stuck note
                return stuck_note(key)
            if fingerprint:
                page_state["fingerprint"] = fingerprint
            loop["last_key"], loop["last_fp"], loop["progress_streak"] = key, fingerprint, 0
        elif fingerprint:
            page_state["fingerprint"] = fingerprint
        return result

    async def screenshot() -> dict:
        b64 = await browser.screenshot_b64()
        # image_b64 is the loop's vision protocol: the loop attaches this PNG as an image_url part for Qwen
        return {"image_b64": b64, "note": "the viewport image is attached"}

    async def download(index: int | None = None, selector: str | None = None) -> dict:
        got = await _call("download", 90.0, budget, browser.download, index, selector)
        if "error" in got:
            return got
        record = store.add("document", site=site_id, title=got.get("name"), url=browser.last_url or None,
                           meta={"sha256": got.get("sha256")}, data=got.get("bytes"))
        return {"rid": record.get("rid"), "name": got.get("name"), "size": got.get("size")}

    async def pdf_texts(rid: str, pages: list[int] | None = None) -> dict:
        data = store.bytes_of(rid)
        if data is None:
            return {"error": f"no stored bytes under rid {rid}"}
        documents = sys.modules.get("atira_agent.documents") or importlib.import_module(".documents", __package__)
        return await _call("pdf_texts", TOOL_TIMEOUT["document"], budget, documents.pdf_texts, data, pages)

    async def pdf_render(rid: str, page: int = 0) -> dict:
        data = store.bytes_of(rid)
        if data is None:
            return {"error": f"no stored bytes under rid {rid}"}
        documents = sys.modules.get("atira_agent.documents") or importlib.import_module(".documents", __package__)
        png = await _call("pdf_render", TOOL_TIMEOUT["document"], budget, documents.pdf_render_png, data, int(page))
        if isinstance(png, bytes):  # the loop attaches image_b64 as an image_url part for Qwen vision
            return {"image_b64": base64.b64encode(png).decode(), "note": f"page {page} of the document, as an image"}
        return png

    async def zip_list(rid: str) -> Any:
        data = store.bytes_of(rid)
        if data is None:
            return {"error": f"no stored bytes under rid {rid}"}
        documents = sys.modules.get("atira_agent.documents") or importlib.import_module(".documents", __package__)
        return await _call("zip_list", TOOL_TIMEOUT["document"], budget, documents.zip_entries, data)

    async def zip_read_text(rid: str, name: str, index: int | None = None) -> Any:
        data = store.bytes_of(rid)
        if data is None:
            return {"error": f"no stored bytes under rid {rid}"}
        documents = sys.modules.get("atira_agent.documents") or importlib.import_module(".documents", __package__)
        text = await _call("zip_read", TOOL_TIMEOUT["document"], budget, documents.zip_read, data, name,
                           index=index)
        if isinstance(text, bytes):
            return text.decode("utf-8", errors="replace")
        return text

    async def evidence_add(kind: str, ref: str | None = None, title: str | None = None, revision: str | None = None,
                           dated: str | None = None, authority: str | None = None, text: str | None = None,
                           site_auto: int = 1) -> dict:
        try:
            keep_site = bool(int(site_auto))
        except (TypeError, ValueError):
            keep_site = str(site_auto).lower() not in {"0", "false", "no", ""}
        record = store.add(kind, site=site_id if keep_site else None, ref=ref, title=title,
                           url=browser.last_url or None, revision=revision, dated=dated, authority=authority, text=text)
        return {"rid": record.get("rid")}

    async def evidence_snapshot(kind: str | None = None, contains: str | None = None,
                                ref: str | None = None) -> Any:
        return store.snapshot(kind=kind, contains=contains, ref=ref, limit=60)

    async def evidence_find(ref: str, authority: str | None = None, not_after: str | None = None) -> Any:
        return store.find_latest(ref=ref, authority=authority, not_after=not_after)

    async def evidence_invalidate(rid: str, reason: str) -> Any:
        return {"invalidated": store.invalidate(rid, reason)}

    async def evidence_supersede(winner_rid: str, loser_rid: str, reason: str = "superseded") -> Any:
        return {"superseded": store.supersede(winner_rid, loser_rid, reason)}

    async def evidence_conflicts() -> Any:
        return store.conflicts()

    async def credentials() -> dict:
        creds = await browser.credentials()
        if not creds:
            creds = dict(entry.get("credentials") or {})
        return creds or {"error": "this site has no credentials in the run access"}

    async def site_credentials(site_id_query: str) -> dict:  # schema key `site_id` reaches this via the wrapper
        for site_other in all_sites:
            if site_other["site_id"] != site_id_query:
                continue
            return dict(site_other["entry"].get("credentials") or {}) or {"error": "no credentials for that site"}
        return {"error": f"unknown siteId {site_id_query!r}"}

    async def site_credentials_tool(site_id: str) -> dict:  # parameter name == schema key, exactly
        return await site_credentials(site_id)

    async def other_sites() -> Any:
        return [{"siteId": other["site_id"], "url": other["url"], "role": other["role"]}
                for other in all_sites if other["site_id"] != site_id]

    async def dismiss_popups() -> dict:
        return {"dismissed": await browser.dismiss_popups()}

    async def finish_tool(summary_json: str = "") -> dict:
        # the investigator contract is finish(summary_json); honor it exactly (see _finish_handler)
        return await _finish_handler("summary_json")(summary_json=summary_json)

    B, D, E = TOOL_TIMEOUT["browser"], TOOL_TIMEOUT["document"], TOOL_TIMEOUT["evidence"]
    def stopped() -> dict | None:
        return stop_check_now()

    # measured failure mode: an investigator repeating the same three clicks forever while the page sat
    # unchanged. Per browser action we emit a safe diagnostic (tool/action/page-fingerprint/shape counts —
    # never payload, page text or credentials), and when the same action leaves the page fingerprint unchanged
    # again and again, the helper returns a forced re-observe or replan note instead of letting the model burn
    # its step cap on an identical loop.
    page_state = {"fingerprint": None}

    def evidence_counts() -> Any:
        # a few bounded filter COUNTS (never record content): what the shared store holds for diagnostics
        try:
            return {"total": len(store.snapshot(limit=500)), "docs": len(store.snapshot(kind="document",
                                                                                         limit=500))}
        except Exception:  # pragma: no cover - a duck-typed store may be minimal
            return None

    def track(result: Any, tool_name: str, **labels: Any) -> Any:
        try:
            fingerprint = _page_fingerprint(result)
            if fingerprint:
                page_state["fingerprint"] = fingerprint
            shape = _observed_shape(result)
            _emit("tool_event", agent=site_id, tool=tool_name,
                  action=str(labels.get("action", labels.get("url", "")))[:60],
                  page=fingerprint, evidence=evidence_counts(),
                  **({"shape": shape} if shape else {}))
        except Exception:  # pragma: no cover - diagnostics must never break the tool
            pass
        return result

    def content_fingerprint(observed: Any) -> str:
        """A hash of the page's CONTENT (normalized text + field values + validation/decline lines).

        A same-length rewrite, a filled field or a new validation line all move this fingerprint; a ticking
        timer in unchanged content does not. Never emitted: it is compared, not logged."""
        if not isinstance(observed, dict):
            return ""
        try:
            fields = ",".join(f"{item.get('name', '')}={item.get('value_hint', '')}"
                              for item in (observed.get("elements") or [])[:300]
                              if isinstance(item, dict))
            lines = []
            for line in str(observed.get("text", "") or "").splitlines():
                low = line.lower()
                if any(word in low for word in DECLINE_WORDS):
                    lines.append(line)
            digest = hashlib.sha1(f"{observed.get('url', '')}|{' '.join(str(observed.get('text', '') or '').split())}"
                                  f"|{fields}|{';'.join(lines)}".encode()).hexdigest()[:16]
        except Exception:  # pragma: no cover - a duck-typed observation may be sparse
            return ""
        return digest

    # repeated action + unchanged page -> the model must re-observe, then change strategy (not repeat the loop).
    # Meaningful progress is an OBSERVED change (content hash, a field holding a new value, new evidence) —
    # not a call count. 3 ineffective repeats force a re-observe; 8 stuck rounds force a replan; recovery is
    # bounded: 5 rounds per strategy, 2 failed recoveries overall, then the site is blocked with its evidence.
    loop = {"last_key": "", "last_fp": "", "progress_streak": 0, "recoveries": 0, "blocked": False}

    def stuck_note(key: str) -> dict:
        loop["progress_streak"] += 1
        if loop["progress_streak"] >= NO_PROGRESS_REPLAN_AFTER or loop["recoveries"] >= MAX_RECOVERY_STRIKES:
            loop["blocked"] = True  # recovery exhausted: stop investigating here, keep what evidence holds
            _emit("investigator_blocked", site=site_id, action=key[:80], rounds=loop["progress_streak"])
            return {"actioned": False, "changed": False, "status": "blocked",
                    "note": "Recovery attempts are exhausted for this site: the page stopped responding to "
                            "every strategy. Record what you have and call finish with what is open — your "
                            "evidence is kept and a focused follow-up wave may continue here."}
        if loop["progress_streak"] >= NO_PROGRESS_REOBSERVE_AFTER:
            loop["recoveries"] += 1
            _emit("loop_recovered", agent=site_id, action=key[:80], streak=loop["progress_streak"],
                  force="reobserve", recovery=loop["recoveries"])
            return {"actioned": True, "changed": False,
                    "note": "This repeat action left the page unchanged. Call observe() (or screenshot()) now, "
                            "look at what actually changed (validation lines, field values), then choose a "
                            "DIFFERENT next action. Repeating this click changes nothing."}
        _emit("tool_repeat", agent=site_id, action=key[:80], streak=loop["progress_streak"])
        return {"actioned": True, "changed": False,
                "note": "Same action again with no page change — observe before acting."}

    def T(desc, props, fn, limit=E):  # noqa: N802 - tool factory
        async def gated(**kwargs):
            early = stopped()
            if early is not None:  # the wall deadline outranks the model: finish with what evidence holds
                return early
            return await _dispatch(fn, min(limit, budget.timeout(limit)), **kwargs)

        gated.__signature__ = inspect.signature(fn)  # the wrapper's public face is the helper's own parameters
        return _tool(gated, desc, props, limit)
    S = {"type": "string"}
    N = {"type": "integer"}
    tools = {
        "goto": T("Navigate this site's browser to a URL you chose (start at this site's own URL).", {"url": S},
                  goto, B),
        "observe": T("Re-read the current page: compressed visible text + indexed elements.", {}, observe, B),
        "act": T("One action on the page: click/fill/press/select an element index (or selector); scroll dy px; "
                 "back; wait ms; accept_cookies.",
                 {"action": S, "index": N, "value": S, "selector": S}, act, B),
        "screenshot": T("Attach the viewport as an image when text is not enough.", {}, screenshot, B),
        "dismiss_popups": T("Close cookie walls/dialogs via generic close/accept affordances.", {}, dismiss_popups,
                            B),
        "download": T("Click the indexed link and capture the download into evidence (returns rid, name, size).",
                      {"index": N, "selector": S}, download, 90.0),
        "pdf_texts": T("Text of a stored document's pages: pdf_texts(rid, pages?).", {"rid": S, "pages":
                      {"type": "array", "items": N}}, pdf_texts, D),
        "pdf_render": T("Look at one document page as an image.", {"rid": S, "page": N}, pdf_render, D),
        "zip_list": T("List a stored zip's entries.", {"rid": S}, zip_list, D),
        "zip_read_text": T("Read one safe zip entry as text.", {"rid": S, "name": S}, zip_read_text, D),
        "evidence_add": T("Record a durable fact/page/mail with ref, revision, dated, authority and exact text.",
                          {"kind": S, "ref": S, "title": S, "revision": S, "dated": S, "authority": S, "text": S,
                           "site_auto": N}, evidence_add),
        "evidence_snapshot": T("Recent evidence records (kind/contains/ref filters).", {"kind": S, "contains": S,
                               "ref": S}, evidence_snapshot),
        "evidence_find": T("Latest active record with this exact ref, optionally by authority and not_after date.",
                           {"ref": S, "authority": S, "not_after": S}, evidence_find),
        "evidence_invalidate": T("Mark a superseded record inactive, with a reason.", {"rid": S, "reason": S},
                                 evidence_invalidate),
        "evidence_supersede": T("Mark the loser inactive in favour of the winner.", {"winner_rid": S,
                                  "loser_rid": S, "reason": S}, evidence_supersede),
        "evidence_conflicts": T("Active records that clash (different revision/dated/text on the same ref).", {},
                                evidence_conflicts),
        "credentials": T("This site's run login (use only where this site's page asks).", {}, credentials),
        "site_credentials": T("The run login of another site, when the brief says this site needs it.",
                              {"site_id": S}, site_credentials_tool),
        "other_sites": T("The campaign's other websites: {siteId,url,role}.", {}, other_sites),
        "finish": T("Done: finish(summary_json) = {\"summary\": ..., \"records\": [...]}.",
                    {"summary_json": S}, finish_tool),
    }
    system = _investigator_system(site, all_sites, missions, brief, answer_schema, schema_keys)
    agent = QwenAgent(f"investigator:{site_id}", system, tools, budget=budget)
    task_line = (f"Investigate {site['url']} (siteId {site_id}, role {site['role']}). Record what the brief needs "
                 "from this site into evidence, then finish.")
    if focus:
        task_line = f"{task_line}\n{focus}"
    started = time.monotonic()
    max_steps = FOCUS_WAVE_STEPS if focus else 60  # a follow-up wave is focused, not a fresh browse
    # one agent loop, hard-bounded twice over: the shared tool gate above, and this slice per investigator agent
    slice_s = max(20.0, min(300.0 if focus else INVESTIGATOR_MAX_SECONDS,
                            budget.remaining() - budget.reserve_seconds - INVESTIGATOR_STOP_SLACK))
    try:
        result = await asyncio.wait_for(agent.run(task_line, max_steps=max_steps), timeout=slice_s)
    except asyncio.TimeoutError:
        _emit("investigator_stopped", site=site_id, reason="slice")
        return {"summary": "stopped: this investigator used up its slice; evidence stands", "records": [],
                "status": "phase_timeout", "stopped": "slice", "unresolved": [site_id],
                "blocker": "the investigator slice ended mid-work"}
    except Exception as exc:
        _emit("investigator_error", site=site_id, error=type(exc).__name__)
        return {"summary": None, "error": str(exc)[:400], "status": "error", "records": [],
                "unresolved": [site_id], "blocker": f"the investigator crashed: {type(exc).__name__}"}
    result = dict(result or {})
    result.setdefault("summary", None)
    # the worker's EXPLICIT status (never inferred from vibes): complete, step_limit, or error
    if result.get("error") and not result.get("finished", True):
        result["status"] = "step_limit"
        result.setdefault("unresolved", [])
        result.setdefault("blocker", "reached the step limit without finishing")
    elif "error" in result:
        result["status"] = "error"
        result.setdefault("unresolved", [])
        result.setdefault("blocker", str(result.get("error"))[:200])
    else:
        result["status"] = "complete"
    _emit("investigator_done", site=site_id, seconds=round(time.monotonic() - started, 1),
          status=result["status"], records=len(result.get("records") or [])
          if isinstance(result.get("records"), list) else None)
    return result


# ---- synthesis -----------------------------------------------------------------------------------------------

# internal provenance keys: real if a helper adds them, stripped before the candidate reaches any answer
_META_KEYS = ("provenance", "_campaign_decision", "_decision_state", "_no_bid_provenance")


def _strip_meta_fields(candidate):
    """Remove internal provenance keys from the candidate (the portal answer carries schema fields + notes only).

    Provenance is NOT removed by decision or position count: a no-bid's evidence refs ride in the campaign's
    decision state, which the finalizer reads. This is a belt-and-braces strip of any stray internal key."""
    if not isinstance(candidate, dict):
        return candidate
    out = {key: value for key, value in candidate.items() if key not in _META_KEYS}
    return out


def decision_support(store, campaign: dict | None, refs: list[str]) -> tuple[bool, list[dict], list[str]]:
    """Check claimed evidence ids against the shared store — existence only, deterministic and generic.

    Returns (all_exist, checked_rows, unknown_ids). A no-bid's support is a SEMANTIC judgement the model owns:
    the code verifies the ids are real ACTIVE records (never a keyword scan, never a reason-string assertion),
    and hands the rows back so the model reads them and attests to the meaning itself.
    """
    wanted = [str(r) for r in (refs or []) if str(r).strip()]
    rows = store.snapshot(active_only=True, limit=2000) if store is not None else []
    known = {str(row.get("rid")): row for row in rows}
    checked, unknown = [], []
    for rid in wanted:
        row = known.get(rid)
        if row is None:
            unknown.append(rid)
        else:
            checked.append({"rid": rid, "kind": row.get("kind"), "ref": row.get("ref"),
                            "revision": row.get("revision"), "dated": row.get("dated")})
    return not unknown, checked, unknown


def _RUNTIME_BUDGET() -> RuntimeBudget:  # replaced by run_campaign's shared budget (set at campaign start)
    return RuntimeBudget()


_BUDGET_HOLDER: dict = {"get": _RUNTIME_BUDGET}


async def review_no_bid(store, campaign: dict | None, brief: str, candidate: dict,
                        budget: RuntimeBudget | None = None) -> dict:
    """A bounded model review that can ESTABLISH no-bid support from real evidence ids — or refuse it.

    Used when no upstream support exists for a no-bid: Qwen reads the store's rows itself, chooses which ids
    support "no active exact technical match", and re-attests. The code validates ids exist and stay active
    (a revoked ref does not establish a decision); WHAT the evidence means stays entirely the model's call.
    """
    props = {"question": {"type": "string"}}
    finish = _finish_handler("result_json")

    async def evidence_all() -> Any:
        return store.snapshot(active_only=True, limit=2000)

    async def evidence_rows(ref: str | None = None, kind: str | None = None) -> Any:
        return store.snapshot(ref=ref, kind=kind, active_only=True, limit=200)

    async def evidence_row(rid: str) -> Any:
        for row in store.snapshot(active_only=True, limit=2000):
            if str(row.get("rid")) == str(rid):
                return row
        return {"error": f"no active record with rid {rid!r}"}

    def checked_finish():
        async def finish_review(result_json: str = "") -> dict:
            parsed = await finish(result_json=result_json)
            if isinstance(parsed, dict) and parsed.get("decision") == "no_bid" \
                    and isinstance(parsed.get("supporting_refs"), list):
                ok, rows, unknown = decision_support(store, campaign, parsed["supporting_refs"])
                if not ok:
                    return {"error": f"unknown or inactive rid(s): {unknown}; re-read evidence_all() and re-attest"}
                parsed["checked"] = rows
            return parsed

        return finish_review

    tools = {
        "evidence_all": _tool("Every active evidence record (the campaign's whole recorded facts).", {},
                              evidence_all, 15.0),
        "evidence_rows": _tool("Active records by ref/kind.", {"ref": {"type": "string"},
                                                               "kind": {"type": "string"}}, evidence_rows, 15.0),
        "evidence_row": _tool("One active record by rid.", {"rid": {"type": "string"}}, evidence_row, 15.0),
        "finish": Tool("finish(result_json) = {\"decision\": \"no_bid\", \"supporting_refs\": [rid, ...], "
                       "\"reason\": \"<what you read>\"} when the evidence supports it; otherwise "
                       "{\"decision\": \"<the decision you can actually support>\", \"reason\": \"...\"}.",
                       {"type": "object", "properties": {"result_json": {"type": "string"}}}, checked_finish()),
    }
    system = (
        "You review whether a no_bid decision is supported by the campaign's shared evidence. Decide from what "
        "the records actually say: open them, read the technical requirements and the material/specification "
        "rows, and judge whether any ACTIVE record shows an exact technical match was possible. If none does, "
        "finish with decision no_bid and list the rid(s) that establish it — every id must be a real active "
        "record you actually read. If the evidence does NOT support a no-bid, finish with the decision the "
        "evidence does support instead. Never invent an id or a fact. Never call a missing fact a no-match: a "
        "gap in research is a gap, not a confirmed no-match.\n"
        f"CALCULATOR / CAMPAIGN STATE: {_dump((campaign or {}).get('decision') or {}, cap=800)}\n"
        f"Prepared candidate (decision under review):\n{_dump(candidate, cap=4000)}\n"
        f"RUN BRIEF (authoritative):\n{brief}"
    )
    agent = QwenAgent("decision_review", system, tools,
                      budget=budget if budget is not None else _BUDGET_HOLDER["get"](),
                      max_output_tokens=4096)
    try:
        result = await asyncio.wait_for(agent.run("Decide what the evidence supports and attest it.",
                                                  max_steps=10), timeout=120.0)
    except Exception as exc:
        return {"error": f"the review did not finish: {type(exc).__name__}"}
    refs = result.get("supporting_refs") if isinstance(result, dict) else None
    if isinstance(result, dict) and str(result.get("decision", "")).lower().replace("-", "_") == "no_bid" \
            and isinstance(refs, list) and refs:
        ok, rows, unknown = decision_support(store, campaign, refs)
        if ok:
            return {"supported": True, "refs": [row["rid"] for row in rows], "reason": str(result.get("reason"))[:300]}
        return {"error": f"attested ids not all active/real: {unknown}"}
    return {"error": f"the review did not establish no_bid: decision={str(result.get('decision'))[:30]!r}"}





async def _synthesize(brief: str, answer_schema: dict, store, budget: RuntimeBudget, kind: str,
                      pricing_module=None, campaign: dict | None = None) -> dict | None:
    """Price the campaign with Qwen and return the calculator's candidate (plus provenance into `campaign`).

    Returns: {"kind","answer","provenance"} on a calculator success — the answer is ONLY ever calculator-
    produced (a model paraphrase never counts); {"kind":"partial","answer",...} for a budget-forced best
    effort; None honestly when nothing could be priced. `campaign["decision"]` receives the provenance
    (calculator no-bid path, later the finalizer's attested refs) — internal state, never the portal answer."""
    # looked up by name first: a test's stub in sys.modules must win over any stale package attribute binding
    pricing = pricing_module if pricing_module is not None else sys.modules.get("atira_agent.pricing")
    if pricing is None or not hasattr(pricing, "calculate_bid"):
        try:
            pricing = importlib.import_module(".pricing", __package__)
        except Exception as exc:
            _emit("pricing_unavailable", error=type(exc).__name__)
            return None

    state = {"success": None, "final": None, "errors": [], "payload": None}
    # internal metadata (never portal-visible): WHY the answer is what it is — the calculator's own no-bid path,
    # or the evidence the finalizer attests support a model-decided no-bid
    provenance = {"kind": "", "error_path": "", "evidence_refs": []}

    def calculate(payload: dict) -> dict:
        result = pricing.calculate_bid(payload)
        state["success"] = result
        # a completed calculator answer is kept: the finish tool returns it when the model finishes empty, and a
        # later unpriced finish text can never replace a calculator-produced candidate
        state["final"] = dict(result.get("answer") or {})
        answer = state["final"]
        if str(answer.get("decision", "")).lower().replace("-", "_") == "no_bid":
            # the calculator's own no-bid (Rules 3/8: a position without an exact active material match):
            # which position it names rides along as provenance (internal, never portal-visible), so the
            # no-bid is never silently rejected for lacking support — the ids are real payload positions
            unmatched = [index for index, item in enumerate(payload.get("positions") or [])
                         if isinstance(item, dict) and item.get("material") is None]
            provenance["kind"] = "calculator_no_bid"
            provenance["error_path"] = ",".join(
                str((payload.get("positions") or [])[i].get("position_id") or f"positions.{i}")
                for i in unmatched[:8])
        _emit("pricing_ok", paths=0, decision=str(answer.get("decision"))[:20])
        return result

    # the inline snapshot in the system prompt is the primary evidence view; the three evidence tool handlers
    # share ONE cap of 3 calls, after which they return a repairable error directing the model to pricing_calculate
    evidence_calls = {"n": 0}

    def _evidence_capped() -> Any:
        evidence_calls["n"] += 1
        if evidence_calls["n"] > 3:
            return {"error": "evidence tool cap reached (3 calls shared): the inline evidence snapshot in the "
                             "system prompt is already in front of you; build the payload and call "
                             "pricing_calculate now", "path": None}
        return None

    async def evidence_snapshot(kind: str | None = None, contains: str | None = None,
                                ref: str | None = None) -> Any:
        capped = _evidence_capped()
        if capped is not None:
            return capped
        return store.snapshot(kind=kind, contains=contains, ref=ref, limit=80)

    async def evidence_stats() -> Any:
        capped = _evidence_capped()
        if capped is not None:
            return capped
        return store.stats()

    async def evidence_conflicts() -> Any:
        capped = _evidence_capped()
        if capped is not None:
            return capped
        return store.conflicts()

    async def pricing_calculate(payload: str | dict = "") -> Any:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload or "")
            except ValueError as exc:
                return {"error": f"payload is not valid JSON: {exc}", "path": None}
        if not isinstance(payload, dict):
            return {"error": "payload must be a JSON object", "path": None}
        state["payload"] = payload  # what the model last asked the calculator about: the forced-fallback basis
        if len(state["errors"]) >= 3:
            return {"error": "fix limit reached; the last errors stand: " + json.dumps(state["errors"][-3:]),
                    "path": state["errors"][-1].get("path")}
        try:
            return calculate(payload)  # pure and sync; _tool runs it on a worker thread, bounded by its limit
        except pricing.PricingError as exc:
            item = {"error": exc.message, "path": exc.path}
            state["errors"].append(item)
            _emit("pricing_error", path=exc.path)
            return item
        except asyncio.TimeoutError:
            return {"error": "the calculator did not finish; simplify and retry once", "path": None}

    E = TOOL_TIMEOUT["evidence"]
    T = lambda desc, props, fn, limit=E: _tool(fn, desc, props, limit)  # noqa: E731
    S = {"type": "string"}
    tools = {
        "evidence_snapshot": T("The evidence investigators recorded.", {"kind": S, "contains": S, "ref": S},
                               evidence_snapshot),
        "evidence_stats": T("Evidence store counts.", {}, evidence_stats),
        "evidence_conflicts": T("Evidence records that clash.", {}, evidence_conflicts),
        # the calculator may run long: it gets its own generous slice, budget-capped by the loop
        "pricing_calculate": T("Run the campaign calculator on your payload; returns the result (answer+derived) "
                               "or {\"error\", \"path\"}.", {"payload": {"type": "string"}}, pricing_calculate,
                               90.0),
        # the calculator already answered (or the model must not invent one): the loop keeps this result
        "finish": Tool("Done: finish(result_json) with the calculator's returned answer as a JSON string.",
                       {"type": "object", "properties": {"result_json": S}},
                       _finish_handler("result_json", state)),
    }
    stats, conflicts = {}, []
    try:
        stats, conflicts = store.stats(), store.conflicts()
    except Exception:  # pragma: no cover - a duck-typed store may be minimal
        pass
    # bounded active snapshot appended inline to the system prompt: max 40 rows, aggregate <= 60000 characters,
    # each row's text <= 8000 characters, so synthesis starts with the evidence in front of it (14 steps of
    # evidence-reading left no room for the calculator)
    snapshot = ""
    try:
        lines = []
        total = 0
        for row in (store.snapshot(limit=40) or []):
            text = _dump(row, cap=8000)
            if total + len(text) > 60000:
                break
            lines.append(text)
            total += len(text)
        snapshot = "\n".join(lines)[:60000]
    except Exception:  # pragma: no cover - a duck-typed store may be minimal
        snapshot = ""
    system = _synthesis_system(brief, getattr(pricing, "PAYLOAD_SCHEMA_DOC", ""), answer_schema, stats, conflicts,
                               snapshot)
    agent = QwenAgent("synthesis", system, tools, budget=budget, max_output_tokens=8192)
    # what the phase may take, in TWO caps: the phase's own share (the remaining budget minus what later phases
    # keep, at least SYNTHESIS_MIN_SLICE while that fits — measured failure: 14 steps of evidence-reading, no
    # calculator call, 823 s unused) and the hard wall (remaining minus the finalizer's keep) as the outer bound.
    # the finalizer's keep is never a fixed 30 s: it is at least the budget's own reserve, so site synthesis
    # cannot squeeze the finalizer below what the budget promised it
    keep = max(FINALIZER_SLACK, budget.reserve_seconds) if kind == "site" else 10.0
    remaining = budget.remaining()
    # the phase's own share: at least SYNTHESIS_MIN_SLICE while it fits the budget (measured failure: a 60 s
    # share squeezed the calculator out with hundreds of seconds free); deep inside the reserve the phase
    # takes what remains at all, because a forced fallback must still get its chance
    share = min(max(SYNTHESIS_MIN_SLICE, remaining - keep), remaining) if remaining > keep \
        else max(remaining, 20.0)
    wall = max(20.0, remaining - keep)  # the hard wall: what later phases keep is never this phase's to spend
    forced = False
    try:
        result = await asyncio.wait_for(agent.run("Build the payload, price it, and finish with the answer.",
                                                  max_steps=14), timeout=min(share, wall))
    except asyncio.TimeoutError:
        forced, result = True, {}
        _emit("synthesis_stopped", error="TimeoutError")
    except Exception as exc:
        # the slice itself expiring (or any stop) is the "budget forces finalize" case: the fallback below uses
        # the payload the model last handed the calculator and prices what remains after omitting the rejected
        # positions; with nothing recorded, the fallback returns None honestly.
        forced, result = True, {}
        _emit("synthesis_stopped", error=type(exc).__name__)
    if isinstance(result, dict) and result.get("error"):
        # a step-limit stop or tool error is the same budget-forced case: let the existing calculator-based
        # partial recovery run instead of losing the last submitted payload
        forced = True
    notes: list[str] = []
    if state["errors"]:
        notes.append("calculator rejected the payload: " + "; ".join(
            f"{e.get('path')}: {e.get('error')}" for e in state["errors"][-3:])[:900])
    if state["success"] is not None:
        # a completed calculator run outranks any unpriced finish text: keep the priced candidate, never a model
        # paraphrase that did not come from the calculator
        candidate = dict(state["success"].get("answer") or {})
        notes.append("partial bid: the answer finished after the calculator succeeded; positions kept as priced.")
        candidate["notes"] = ("; ".join(notes))[:1500]
        return {"kind": "priced", "answer": candidate or None, "provenance": provenance} or None
    if result and isinstance(result, dict) and not forced and not result.get("error"):
        # the model finished without a calculator success: nothing priced, invent nothing — but a model-
        # ATTESTED no-bid (real rid refs against this store) is a legitimate final candidate: the campaign's
        # semantic decision, not a calculator artifact. Ids are verified deterministically; meaning is the model's.
        if str(result.get("decision", "")).lower().replace("-", "_") == "no_bid" \
                and isinstance(result.get("supporting_refs"), list) and result["supporting_refs"]:
            ok, rows, unknown = decision_support(store, campaign, result["supporting_refs"])
            if ok:
                candidate = {"decision": "no_bid", "tender_reference": result.get("tender_reference"),
                             "positions": [], "total_net": 0.0, "currency": "EUR",
                             "notes": str(result.get("reason") or "no active exact technical match: attested")
                             [:400]}
                provenance["kind"] = "attested_no_bid"
                provenance["evidence_refs"] = [row["rid"] for row in rows]
                _emit("no_bid_attested", refs=len(rows))
                return {"kind": "priced", "answer": candidate, "provenance": provenance}
            _emit("no_bid_attestation_invalid", unknown=unknown[:6])
            return None
        _emit("synthesis_unpriced", keys=sorted(result)[:8])
        return None
    candidate = (_partial_candidate(result, notes, state["errors"], state["payload"],
                                    (lambda payload: calculate(payload))) if forced else None)
    if candidate:
        notes.append("partial bid: the full calculation did not complete; unfinished positions are omitted and "
                     "may be entered as omitted on the form (Teilpunkte).")
        candidate["notes"] = "; ".join(notes)[:1500]
        return {"kind": "partial", "answer": candidate, "provenance": provenance}
    return None


def _partial_candidate(result: dict, notes: list[str], errors: list[dict], payload: dict | None = None,
                       calculate=None) -> dict | None:
    """A budget-forced best effort: the last candidate the synthesis model itself produced, if any.

    With the calculator and the payload the model last submitted, the fallback prices what it can: positions the
    calculator rejected are omitted entirely and the remainder is re-priced, so the partial bid stays honest."""
    if calculate is not None and payload is not None:
        try:
            result = calculate(dict(payload))
        except Exception as exc:
            notes.append(f"full payload did not calculate: {exc}")
            result = None
    candidate = None
    if isinstance(result, dict):
        for key in ("candidate", "answer", "summary"):
            value = result.get(key)
            if isinstance(value, dict):
                candidate = dict(value)
                break
            if isinstance(value, str) and value.strip().startswith("{"):
                try:
                    parsed = json.loads(value)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    candidate = parsed
                    break
    if candidate is None or not isinstance(candidate, dict):
        return None
    positions = candidate.get("positions")
    if isinstance(positions, list) and errors:
        bad_paths = {e.get("path") for e in errors if e.get("path")}
        keep = []
        for index, position in enumerate(positions):
            name = str(position.get("position", "")) if isinstance(position, dict) else ""
            if any(str(path).startswith(f"positions.{index}.") or (name and name in str(path)) for path in bad_paths):
                notes.append(f"omitted position {name or index}: calculator reported {name or index} invalid")
                continue
            keep.append(position)
        candidate["positions"] = keep
    # with the calculator at hand, price what remains after omitting the broken positions entirely
    if calculate is not None and payload is not None and isinstance(payload.get("positions"), list):
        bad_paths = {e.get("path") for e in errors if e.get("path")}
        while True:
            try:
                priced = calculate(dict(payload))
                candidate = dict(priced.get("answer") or candidate)
                break
            except Exception as exc:  # PricingError: drop its position and try the remainder
                path = str(getattr(exc, "path", "") or "")
                match = re.match(r"positions\.(\d+)", path)
                if not match or not payload["positions"]:
                    break
                dropped = payload["positions"].pop(int(match.group(1)))
                omitted = dropped.get("position_id") or dropped.get("position") or "?"
                notes.append(f"omitted position {omitted}: {getattr(exc, 'message', exc)}")
                bad_paths.add(path)
    return candidate


def _decline_shown(observed: dict) -> bool:
    """Whether a fresh observation carries a generic decline/validation-failure line (generic words only).

    The words are universal UI vocabulary, not scenario facts; WHAT the page objects to stays the model's
    decision — this only refuses a success claim while the page visibly says no."""
    text = f"{observed.get('text', '')} {observed.get('title', '')}".lower()
    return any(word in text for word in DECLINE_WORDS)


# ---- site finalizer ------------------------------------------------------------------------------------------


async def _finalize_site(brief: str, answer_schema: dict, candidate: dict, sites: list[dict], by_id: dict,
                         store, budget: RuntimeBudget, validation: tuple[bool, list[str]] | None = None,
                         max_attempts: int = 2) -> RunOutcome:
    """Delegate to the submission module (runtime_submission.finalize_site), imported lazily.

    The finalizer lives in its own module and owns its own tests; this wrapper keeps the runtime's private
    call shape until that owner updates the call sites. A missing delegate is reported honestly, never faked."""
    try:
        submission = importlib.import_module(".runtime_submission", __package__)
    except Exception as exc:
        _emit("submission_unavailable", error=type(exc).__name__)
        return RunOutcome(submitted=False, candidate=candidate,
                          reason=f"submission module unavailable: {type(exc).__name__}")
    # max_attempts is accepted for legacy call-shape compatibility only; the actual
    # runtime_submission.finalize_site signature does not support it, so it is NOT forwarded.
    return await submission.finalize_site(brief, answer_schema, candidate, sites, by_id, store, budget,
                                          validation=validation)


# Generic confirmation/decline words any bid-form receipt or rejection shows: patterns, not scenario facts.
# The submission module (its owner) may reuse them; kept here where the runtime's outcome checks read them.
RECEIPT_WORDS = ("bestätigung", "bestätigt", "eingegangen", "received", "submitted", "vielen dank", "vorgang")
DECLINE_WORDS = ("ungültig", "abgelehnt", "fehler", "invalid", "expired", "abgelaufen", "versuch")

# The submission's explicit stages: recorded when the model reports one it actually completed; the order is
# advisory, the page verification after `finish` remains the gate. No stage is ever stamped by the code alone.
STAGES = ("candidate_validated", "form_populated", "fields_verified", "signing_requested",
          "current_code_retrieved", "signing_attempted", "receipt_verified")


async def _receipt_hits(browser) -> bool:
    """Whether the current page shows receipt-shaped lines/fields (patterns generic; the model still reads)."""
    try:
        found = await asyncio.wait_for(browser.find_receipt(
            [re.escape(word) for word in RECEIPT_WORDS]), timeout=15.0)
    except Exception:
        return False
    return bool(found.get("receipt_hits"))


def _page_shows_receipt(observed: dict) -> bool:
    """Whether a fresh observation of the portal shows receipt-like text and no generic decline message.

    The words are generic UI vocabulary; whether a specific line IS the bid's receipt stays the model's decision
    (it reads the page itself); this only refuses a success claim when the current page contradicts it.
    """
    text = f"{observed.get('text', '')} {observed.get('title', '')}".lower()
    shows_decline = any(word in text for word in DECLINE_WORDS)
    return not shows_decline and any(word in text for word in RECEIPT_WORDS)


# The site finalizer agent itself lives in runtime_submission.finalize_site (its own owner and tests);
# _finalize_site above delegates there once that module lands.
