"""Submission finalizer: get a real, signed bid onto the tender portal's own form with a verified receipt.

Priority (human-approved plan): a real partial submission beats endless research. Official bid rules may allow
omitting unfinished positions (observedPosition auslassen / Teilpunkte) — an omitted position is never a fabricated
value and missing research is never a confirmed no-bid. Every navigation, search, click, fill and sign decision is
a hosted-Qwen tool-call; this module only wires model-visible tools to the run's browsers and evidence store,
enforces the budget, tracks explicit stage progress and verifies receipts against a fresh same-origin observation.

Self-contained on purpose: `runtime.py` may lazily delegate its finalizer here, so importing this module must never
import runtime (a circular import would break both). Only `contracts` and `runtime_models` are imported.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import sys
import time
from typing import Any
from urllib.parse import urlparse

from .contracts import RunOutcome, RuntimeBudget
from .runtime_models import QwenAgent, Tool

# explicit, ordered stage names; recorded on the shared state as evidence supports them, never assumed
STAGES = ["candidate_validated", "form_populated", "fields_verified", "signing_requested",
          "current_code_retrieved", "signing_attempted", "receipt_verified"]

TOOL_TIMEOUT = 45.0          # one browser step, also capped by the budget per call
RECOVERY_ROUNDS = 5          # forced observe/screenshot/model-replan rounds after a guard trip
MAX_SIGN_RECOVERIES = 2      # a third failed signing recovery stops this attempt with a preserved blocker
NO_PROGRESS_REPEATS = 3      # same effective action + unchanged page this often -> force a re-observe
NO_PROGRESS_ROUNDS = 8       # this many consecutive rounds without progress -> force a replan

# generic UI vocabulary for a read-only fill proof (which fields actually hold a value, required marks);
# these are field-state questions, never scenario values
_FILL_PROOF_JS = """() => {
  const proof = [];
  for (const el of document.querySelectorAll('input,textarea,select')) {
    if (el.disabled || el.type === 'hidden') continue;
    let state = el.value || '';
    if (el.type === 'checkbox') state = el.checked ? 'on' : '';
    if (el.type === 'radio') state = el.checked ? (el.value || 'on') : '';
    proof.push({name: el.name || el.id || '', type: el.type || el.tagName.toLowerCase(),
                value: String(state).slice(0, 120),
                has_value: String(state).length > 0,
                required: el.required || el.getAttribute('aria-required') === 'true',
                invalid: !!el.validationMessage, validation: el.validationMessage || ''});
  }
  const alerts = [...document.querySelectorAll('[role=alert],output,.error,.invalid,.feld-fehler')]
      .map(n => (n.innerText || '').trim().slice(0, 160)).filter(Boolean).slice(0, 12);
  const dialogs = [...document.querySelectorAll('dialog[open],[role=dialog]')]
      .map(n => (n.innerText || '').trim().slice(0, 160)).filter(Boolean).slice(0, 4);
  const disabled = [...document.querySelectorAll('button[disabled],input[disabled],select[disabled],'
      + 'textarea[disabled]')].map(n => (n.innerText || n.name || n.id || '').trim().slice(0, 60))
      .filter(Boolean).slice(0, 12);
  return {field_proof: proof.slice(0, 40), alerts, dialogs, disabled};
}"""

# generic confirmation / rejection wording any portal might show: patterns, never scenario facts
RECEIPT_WORDS = ("bestätigung", "bestätigt", "eingegangen", "received", "submitted", "vielen dank", "vorgang")
DECLINE_WORDS = ("ungültig", "abgelehnt", "fehler", "invalid", "expired", "abgelaufen")
SECRET_WORDS = ("tan", "einmalpasswort", "passwort", "password", "token", "secret")


def _emit(event: str, **fields: Any) -> None:
    """Log via the events module when present; events carry classifications/counts only, never payloads.
    A logger must never be the reason a run fails, so the logging seam is deliberately total (BLE001)."""
    try:
        events = sys.modules.get("atira_agent.events")
        if events is None:
            import importlib
            events = importlib.import_module(".events", __package__)
    except Exception:  # logging is optional: a missing/broken module stays silent; never the reason a run fails
        return
    try:
        events.emit(event, **fields)
    except Exception:  # the log must never be the reason a run fails (same total-logging-seam reason)
        pass


def _dump(value: Any, cap: int = 12_000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover
        text = str(value)
    return text if len(text) <= cap else text[:cap] + "…[truncated]"


def _bounded(fn, limit: float, /, *args, **kwargs):
    """One browser step bounded by min(step limit, budget left); never longer than the clock allows."""

    async def run():
        return await asyncio.wait_for(fn(*args, **kwargs), timeout=max(0.5, min(limit, 30.0)))

    return run()


def _fallback_problems(candidate: dict, props: dict, required: list, path: str = "") -> list[str]:
    """Checks when no schema validator is importable: keys, types (a bool is never a number/integer), enums,
    const, min/max, nested items/properties. An unsupported construct is an explicit problem — never skipped,
    so an unchecked constraint can never let a candidate pass through a validator gap unnoticed."""
    problems: list[str] = []
    simple = {"string": str, "object": dict, "array": list}
    for key in (required or []):
        if key not in candidate:
            problems.append(f"{path}{key!r} required")
    for key, spec in (props or {}).items():
        here = f"{path}{key}"
        if key not in candidate or not isinstance(spec, dict):
            continue
        value = candidate[key]
        expected = spec.get("type")
        if "const" in spec and value != spec["const"]:
            problems.append(f"{here} != const")
        if isinstance(spec.get("enum"), list) and spec["enum"] and value not in spec["enum"]:
            problems.append(f"{here} not among the schema's enum choices")
        numberish = isinstance(value, (int, float)) and not isinstance(value, bool)
        if isinstance(spec.get("minimum"), (int, float)) and numberish and value < spec["minimum"]:
            problems.append(f"{here} below minimum")
        if isinstance(spec.get("maximum"), (int, float)) and numberish and value > spec["maximum"]:
            problems.append(f"{here} above maximum")
        if expected == "boolean" and not isinstance(value, bool):
            problems.append(f"{here} must be boolean")
        elif expected in {"number", "integer"}:
            if not numberish:
                problems.append(f"{here} must be {expected}")
            elif expected == "integer" and isinstance(value, float) and not value.is_integer():
                problems.append(f"{here} must be an integer")
        elif expected in simple and not isinstance(value, simple[expected]):
            problems.append(f"{here} must be {expected}")
        elif expected is not None and expected not in simple | {"number", "integer", "boolean"}:
            problems.append(f"{here}: unsupported schema type {expected!r} for the fallback checker")
        if expected == "array" and isinstance(value, list) and isinstance(spec.get("items"), dict):
            for index, item in enumerate(value):
                problems.extend(_fallback_problems(item if isinstance(item, dict) else {},
                                                   spec["items"].get("properties") or {},
                                                   spec["items"].get("required") or [], f"{here}[{index}]."))
        elif expected == "object" and isinstance(value, dict):
            problems.extend(_fallback_problems(value, spec.get("properties") or {},
                                               spec.get("required") or [], f"{here}."))
    return problems


def validate_candidate(candidate: Any, answer_schema: Any) -> tuple[bool, list[str]]:
    """Shape-check the candidate against THIS run's answer schema. Guides the model; never fabricates values.

    An intentional omission (the brief's own partial-credit rules) is a legitimate shape: only keys, types and
    declared enums the schema states are checked. A bid with a decision but zero priced positions is not a
    legitimate shape: an empty bid needs the brief's explicit no-bid support, not missing research.
    """
    problems: list[str] = []
    if not isinstance(candidate, dict):
        return False, ["candidate is not an object"]
    schema = answer_schema if isinstance(answer_schema, dict) else None
    if schema and (schema.get("properties") or schema.get("type") or schema.get("required")):
        try:
            import jsonschema  # pinned in pyproject: the full validator when installed

            validator = jsonschema.Draft202012Validator(schema)
            problems = [f"{'/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"[:160]
                        for error in sorted(validator.iter_errors(candidate), key=str)[:12]]
        except ImportError:
            problems = _fallback_problems(candidate, schema.get("properties") or {},
                                          list(schema.get("required") or []))
    decision = str(candidate.get("decision") or "").lower()
    positions = candidate.get("positions")
    if decision.startswith("bid") and not (isinstance(positions, list) and positions):
        problems.append("decision is a bid but positions carry no priced line")
    return not problems, problems


def _rows(store) -> list[dict]:
    try:
        rows = store.snapshot(limit=500) if store is not None else []
    except (AttributeError, TypeError, ValueError):  # a minimal duck-typed store: no rows, never a crash
        return []
    return [row for row in (rows if isinstance(rows, list) else []) if isinstance(row, dict) and row.get("rid")]


def _active_refs(store) -> dict[str, dict]:
    return {str(row["rid"]): row for row in _rows(store)
            if not row.get("invalidated") and not row.get("inactive")}


def no_bid_allowed(candidate: Any, store) -> tuple[bool, str]:
    """A no_bid stands only with a model-confirmed finding plus ACTIVE evidence references — code validates the
    refs exist, the model owns the decision. Never keyword text-matching: a catalog that simply lacks a match
    does not literally say "no_bid", and missing research must not silently become a no-bid.
    """
    if not isinstance(candidate, dict):
        return False, "candidate is not an object"
    decision = str(candidate.get("decision") or "").lower()
    if not decision.startswith("no"):
        return True, ""
    finding = candidate.get("no_bid_finding")
    confirmed = isinstance(finding, dict) and finding.get("confirmed") is True
    reason = str((finding or {}).get("reason") if isinstance(finding, dict)
                 else candidate.get("reason") or candidate.get("notes") or "").strip()
    if not confirmed:
        return False, ("no_bid needs a structured no_bid_finding {confirmed: true, reason, evidence_refs} "
                       "(from the finalizer's evidence review when upstream did not attach one): missing "
                       "research or an unknown is not a confirmed no-bid")
    if not reason:
        return False, "no_bid finding needs a concrete reason"
    refs = (finding or {}).get("evidence_refs")
    refs = [str(r) for r in refs] if isinstance(refs, list) else []
    if not refs:
        return False, "no_bid finding cites no evidence references"
    records = _active_refs(store)
    matched = [ref for ref in refs if ref in records]
    if not matched:
        return False, "no_bid finding's evidence references are not active records in this run's store"
    return True, f"no_bid supported by active evidence refs {matched[:4]}"


def _origin(url: str) -> str:
    try:
        parts = urlparse(str(url))
    except ValueError:  # pragma: no cover
        return ""
    return f"{parts.scheme}|{parts.netloc}".lower()


def _page_signature(observed: Any) -> str:
    """Progress signature: the browser worker's own signature when it exports one; otherwise a normalized
    signature that keeps field values (so a price/quantity/fill change counts) and ignores only incidental
    numeric noise in prose (timers, timestamps), never all numeric content."""
    if not isinstance(observed, dict):
        return ""
    shared = observed.get("observation_signature")
    if isinstance(shared, str) and shared:  # reuse the runtime_browser worker's exported signature when present
        return shared
    words = re.sub(r"\b\d{1,2}[:.]\d{2}(:\d{2})?\b", "#", " ".join(str(observed.get("text", "") or "").lower().split()))
    fields = []
    for item in observed.get("field_proof") or []:
        if isinstance(item, dict):
            # the exact value a field holds (masked to a digest) counts as progress: same-length semantic changes
            # and real fill changes must never be hidden behind a has-value flag alone
            value = str(item.get("value", "")) if "value" in item else ("set" if item.get("has_value") else "")
            fields.append(f"{item.get('name')}:{hashlib.sha1(value.encode()).hexdigest()[:6]}"
                          f"!{int(bool(item.get('invalid')))}")
    alerts = "|".join(str(a).lower()[:60] for a in (observed.get("alerts") or [])[:8])
    elements = len(observed.get("elements") or [])
    title = " ".join(str(observed.get("title", "") or "").lower().split())[:80]
    raw = f"{str(observed.get('url', '')).split('?')[0]}|{title}|{elements}|{words[:4000]}|{'/'.join(fields)}|{alerts}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _text_of(observed: Any) -> str:
    if not isinstance(observed, dict):
        return ""
    return f"{observed.get('title', '')} {observed.get('text', '')}".lower()


def _shows_receipt(observed: Any) -> bool:
    text = _text_of(observed)
    if any(word in text for word in DECLINE_WORDS):
        return False
    lowered = text.lower()
    # Completed affirmative statements only: subject + (optional determiner/copula)
    # + contiguous completed-submission verb. No wildcard between subject and verb.
    patterns = (
        r"(?:ihr|ihre|sein|seine|das|dem|the|your)?\s*"
        r"(?:angebot|bewerbung|einreichung|bid|offer|submission)"
        r"(?:\s+(?:wurde|ist|has been|was))?"
        r"\s+(?:erfolgreich\s+)?"
        r"(?:eingereicht|abgegeben|uebermittelt|übermittelt|eingegangen)",
        r"(?:ihr|ihre|sein|seine|the|your)?\s*"
        r"(?:angebot|bewerbung|einreichung|bid|offer|submission)"
        r"(?:\s+(?:wurde|ist|has been|was))?"
        r"\s+(?:successfully\s+submitted|submitted successfully|received)",
    )
    return any(re.search(p, lowered) for p in patterns)


def _reference_tokens(tender_reference: Any) -> list[str]:
    """The intended tender's full reference, normalized to a lower-case alphanumeric compact form ('-'/'_'/' '
    collapse). An exact substring of the page text is required: a shared token (a year alone) is never a
    match, so a wrong tender that merely shares part of the reference cannot pass."""
    return re.sub(r"[^0-9a-zäöüß]+", "", str(tender_reference or "").lower())


def _bound_to_tender(observed: Any, portal_url: str, claimed: str, tender_reference: Any) -> bool:
    """A fresh observation of THIS portal: same origin, receipt shown with no decline, AND the page text
    carries the intended tender's exact full reference — generic words or partial tokens never succeed."""
    if not isinstance(observed, dict) or not _shows_receipt(observed):
        return False
    page_url = str(observed.get("url") or "")
    if not _origin(page_url) or _origin(page_url) != _origin(portal_url):
        return False
    reference = _reference_tokens(tender_reference)
    if not reference or reference not in re.sub(r"[^0-9a-zäöüß]+", "", _text_of(observed)):
        return False
    if claimed and claimed[:60].lower() not in _text_of(observed):
        return False
    return True


async def verify_receipt(browser, portal_url: str, tender_reference: Any, claimed: str = "",
                        settle: float = 2.0, budget: RuntimeBudget | None = None) -> tuple[bool, str]:
    """Observe the receipt on the intended tender's portal itself, allowing a delayed landing, before any retry.

    Never a false positive: on every path the fresh observation must be at the portal's origin AND carry the
    intended tender's reference in its text; receipt hits from `find_receipt` alone never decide. Every wait is
    bounded by the shared budget (and by `settle`), so a slow confirmation cannot outlive the run.
    """
    slices = (2 if settle else 1)  # two bounded reads when a landing delay is allowed, else one
    for read in range(slices):
        wait = max(1.0, min(30.0, budget.timeout(30.0))) if budget is not None else 30.0
        try:
            observed = await asyncio.wait_for(browser.observe(), timeout=wait)
        except (TimeoutError, ConnectionError, AttributeError, TypeError, ValueError, OSError):  # a slow/dead browser reads as "no receipt", never as a crash
            observed = {}
        if _bound_to_tender(observed, portal_url, claimed, tender_reference):
            return True, str(observed.get("text", ""))[:2000]
        if budget is not None and budget.remaining() <= 1.0:
            break
        if read < slices - 1 and settle:  # a confirmation may land late: wait (bounded), then re-observe
            await asyncio.sleep(min(settle, max(0.0, budget.timeout(settle))) if budget else settle)
    return False, ""


class _Side:
    """One guarded browser (portal or mail): fresh observations, bounded actions, progress and fill accounting.

    `expected` is the field state the model reported via verify_fields; a fields_verified stage mark needs the
    field's ACTUAL observed value to match that expected value (checkboxes/radios compare against 'on'/their
    value) — `has_value` alone never counts as a verified field.
    """

    def __init__(self, name: str, browser, budget: RuntimeBudget, state: dict, url: str):
        self.name, self.browser, self.budget, self.state, self.url = name, browser, budget, state, url
        self.side = {"last_key": "", "last_sig": "", "unchanged": 0}

    async def call(self, kind: str, /, *args, **kwargs) -> Any:
        limit = max(0.5, min(TOOL_TIMEOUT, self.budget.timeout(TOOL_TIMEOUT)))
        try:
            return await asyncio.wait_for(getattr(self.browser, kind)(*args, **kwargs), timeout=limit)
        except TimeoutError:
            return {"error": f"{kind} timed out within the budget slice; re-observe before repeating it"}
        except (ConnectionError, AttributeError, TypeError, ValueError, OSError) as exc:
            return {"error": f"{kind} failed: {type(exc).__name__}"}

    async def observe(self) -> dict:
        result = await self.call("observe")
        if isinstance(result, dict):
            result = {**result, **await self._proof()}
            self.side["last_sig"] = _page_signature(result)
        _emit("submit_observe", side=self.name, sig=_page_signature(result),
              elements=len((result or {}).get("elements") or []) if isinstance(result, dict) else 0)
        return result

    async def _proof(self) -> dict:
        try:
            raw = await asyncio.wait_for(self.browser.page.evaluate(_FILL_PROOF_JS),
                                         timeout=max(0.5, min(15.0, self.budget.timeout(15.0))))
            proof = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except AttributeError:  # a browser stand-in without a real page: plain observation, no fill proof
            return {}
        except (TimeoutError, ConnectionError, TypeError, ValueError, OSError):
            return {}
        return {k: proof.get(k, []) for k in ("field_proof", "alerts", "dialogs", "disabled")}

    async def act(self, action: str, index: int | None = None, selector: str | None = None,
                  value: str | None = None) -> dict:
        action = (action or "").lower()
        key = json.dumps({"action": action, "index": index, "selector": selector}, sort_keys=True)[:120]
        state = self.state
        if (action in {"click", "fill", "press", "select"} and key == self.side["last_key"]
                and self.side["unchanged"] >= NO_PROGRESS_REPEATS and state["recoveries"] >= MAX_SIGN_RECOVERIES):
            return {"actioned": False, "changed": False,
                    "note": "Refused: this unchanged action already spent its recovery rounds. "
                            "Observe fresh or finish with what you saw."}
        result = await self.call("act", action, index, selector, value)
        if not isinstance(result, dict):  # pragma: no cover - defensive for exotic helpers
            result = {"result": str(result)[:2000]}
        proof = await self._proof()  # attach fill proof / alerts / disabled marks (read-only)
        if proof:
            result = {**result, **proof}
        signature = _page_signature(result)
        progress = bool(signature) and signature != self.side["last_sig"]
        changed = action not in {"click", "fill", "press", "select"} or progress or not self.side["last_sig"]
        result["changed"] = bool(changed)
        _emit("submit_action", side=self.name, action=action[:40], changed=bool(changed), sig=signature)
        if action in {"click", "fill", "press", "select"}:
            if key == self.side["last_key"] and signature and not progress and self.side["last_sig"]:
                self.side["unchanged"] += 1
                self.side["last_sig"] = signature
                result["note"] = ("This repeat left the page unchanged. Call observe() now, read the alerts and "
                                  "validation messages, verify which fields actually hold your value, then choose "
                                  "a genuinely different next action.")
                if self.side["unchanged"] >= NO_PROGRESS_REPEATS:
                    result["recovery"] = "forced"
                    _emit("submit_recovery", side=self.name, kind="reobserve", unchanged=self.side["unchanged"])
                return result
            self.side["last_key"], self.side["last_sig"], self.side["unchanged"] = key, signature, 0
        elif signature:
            self.side["last_sig"] = signature
        return result


async def _step_limit(budget: RuntimeBudget) -> float:
    return max(20.0, min(150.0, budget.timeout(150.0)))


def _stage_note(state: dict) -> str:
    done = [name for name in STAGES if state["stages"].get(name)]
    blockers = [b[:200] for b in (state["blockers"][-2:] if state["blockers"] else [])]
    lines = [f"Stages reached so far: {done or 'none'}"]
    if blockers:
        lines.append(f"First blockers already hit: {'; '.join(blockers)}")
        lines.append("Do not repeat what those blockers name: change the route based on what the page says now.")
    return "\n".join(lines)


def _system(brief: str, candidate: dict, answer_schema: Any, tender_reference: str, validation: tuple,
            partial_rules: str) -> str:
    ok, problems = validation
    notes = ""
    if not ok:
        notes = ("Candidate validation notes (" + "; ".join(problems)[:500] + "). The brief may allow omitting "
                 "unfinished positions (observedPosition auslassen / Teilpunkte): a signed partial with a real "
                 "receipt beats an unfinished perfect form. Never invent values for an omitted position.\n")
    else:
        notes = ("If the form rejects a position the brief lets you omit, use its explicit omit/auslassen control "
                 "and submit the partial bid — do not retreat into endless research.\n")
    return (
        "You are the submission finalizer of a tender campaign. The prepared bid answer is below. Enter it into "
        "the tender portal's own bid form: portal_goto the tender page for reference "
        f"{tender_reference or '(read it from the portal page)'} , find the form yourself, fill each field with "
        "portal_act fill using the element index you observed, then verify with portal_verify_fields that every "
        "value landed exactly and read the page's own alerts/validation. Re-observe after every action — pages "
        "change asynchronously. Popups and cookie walls: portal_act accept_cookies or the dismiss control you "
        "observe.\n" + notes +
        "Signing comes LAST and is short-lived: at the signing moment use mail_goto/mail_observe/mail_act to read "
        "the CURRENT signing code from the mail site, enter it, and request_signing. Never reuse an old code, "
        "never store or echo a code in evidence or in your finish text. A rejection is NOT automatically an "
        "expired code: read the actual validation/error text, fix what it names, and only refresh the code when "
        "the page says the code is invalid or expired. Check_receipt FIRST after any rejection or timeout: a "
        "receipt may land late and a receipt means success — never sign twice. On a second attempt, the "
        "progress/blocker summary below tells you what already happened: recover from that state, do not repeat "
        "a blocked route. The brief's rules and the answer schema control the answer; the site's own form is the "
        "submission — never post the answer to an API.\n"
        'finish(result_json) = {"submitted": bool, "receipt": "<receipt text exactly as observed, with the tender '
        'reference>", "reason": "<exact stage and blocker when not submitted>"}.\n'
        f"Prepared answer:\n{_dump(candidate, cap=20_000)}\nAnswer schema: {_dump(answer_schema, cap=2500)}\n"
        f"{partial_rules}\nRUN BRIEF (authoritative):\n{brief}"
    )


def _finish_handler(state: dict, decision_is_no_bid: bool = False, no_bid_supported=None, verify=None):
    """The terminal finish: a claimed submission is confirmed SYNCHRONOUSLY here (fresh observe, inside this
    task's event loop — never a background race) against a same-origin receipt carrying the tender reference.
    Invalid or empty args are repairable, never terminal."""

    async def finish(result_json: str = "") -> Any:
        payload = None
        if result_json:
            try:
                payload = json.loads(result_json) if isinstance(result_json, str) else result_json
            except ValueError:
                return {"error": "result_json is not valid JSON: pass the JSON string the contract describes"}
        if not isinstance(payload, dict):
            return {"error": "finish needs a JSON object {submitted, receipt, reason}"}
        state["finish"] = payload
        # a no_bid decision needs support: upstream candidate evidence, or this run's model attestation over
        # ACTIVE evidence refs — an unknown or missing research is never a confirmed no-bid
        if decision_is_no_bid and payload.get("submitted"):
            allowed, why = (no_bid_supported() if no_bid_supported else (False, "no checker wired"))
            if not allowed:
                return {"error": f"decision no_bid is not supported yet: {why}"}
        if payload.get("submitted") and verify is not None:
            # synchronous, fresh: the precheck's flags are reset so THIS check is the one that decides
            state["receipt_checked"] = False
            result = await verify(settle=1.0, claimed=str(payload.get("receipt") or ""), reason="finish claim")
            if not result.get("receipt_verified"):
                return {"error": "your submitted=true is not confirmed by a fresh tender-bound portal "
                                 "receipt; check_receipt or fix the blocker and finish again"}
            return {"submitted": True, "receipt_verified": True,
                    "note": "receipt confirmed on this tender's portal; finish is accepted"}
        return payload

    return finish


async def finalize_site(brief: str, answer_schema: dict, candidate: dict, sites: list[dict], by_id: dict,
                        store, budget: RuntimeBudget, validation: tuple | None = None,
                        tender_reference: Any = None, partial_rules: str = "") -> RunOutcome:
    """Drive the submission over hosted Qwen: fill, verify, sign at the last moment, confirm the receipt.

    `runtime._finalize_site` may lazily delegate here. Stages and blockers accumulate on a shared state that
    carries into a second signing recovery, so the second attempt never starts from an ignorant context.
    """
    portal = next((s for s in sites if s.get("site_id") == "tender-portal" or s.get("role") == "tender"), None)
    if portal is None:
        return RunOutcome(submitted=False, candidate=candidate, reason="no tender portal in this run")
    mail = next((s for s in sites if s.get("site_id") == "mail" or s.get("role") == "mail"), portal)
    portal_url, mail_url = str(portal.get("url") or ""), str((mail or portal).get("url") or "")
    if tender_reference in (None, ""):
        tender_reference = (candidate or {}).get("tender_reference") or (candidate or {}).get("reference") or ""

    ok, problems = validation if validation is not None else validate_candidate(candidate, answer_schema)
    _emit("candidate_validated", ok=ok, problems=len(problems), phase="finalize")
    no_bid_open = isinstance(candidate, dict) and str(candidate.get("decision") or "").lower().startswith("no")
    # upstream (calculator/answer) support is honored when present; an unconfirmed no-bid stays OPEN so the
    # model's own bounded evidence review inside this phase can establish support — never a premature reject,
    # and never a keyword scan or reason-string assertion by code: the model owns the semantic decision
    no_bid_state = {"ok": False, "why": "not needed"}
    if no_bid_open:
        allowed, why = no_bid_allowed(candidate, store)
        # a no_bid legitimately carries no priced positions: that ONE problem is the decision's own, and it is
        # replaced by the support gate at finish. Every other schema problem stays exactly as found.
        problems = [p for p in problems if "decision is a bid" not in str(p)]
        ok = not problems
        no_bid_state = {"ok": bool(allowed), "why": f"upstream: {why}" if allowed else why}

    state: dict = {"stages": {"candidate_validated": True}, "blockers": [], "recoveries": 0,
                   "last_blocker": "", "last_recovery": "", "receipt": "", "receipt_checked": False,
                   "signings": 0, "expected": {}, "finish": None, "attestation": None}

    def no_bid_supported() -> tuple[bool, str]:
        if no_bid_state["ok"]:
            return True, no_bid_state["why"]
        attestation = state.get("attestation")
        if isinstance(attestation, dict) and attestation.get("confirmed") is True:
            return True, f"model attestation over active refs {attestation.get('evidence_refs', [])[:4]}"
        return False, (state["last_blocker"] or "no_bid decision not yet supported: review_evidence and "
                       "attest_no_bid, or enter the bid the candidate carries")

    # `state["check"]` is set right after check_receipt is defined; finish uses the direct verify reference below
    state["check"] = None

    portal_side = _Side("portal", portal["browser"], budget, state, portal_url)
    mail_side = _Side("mail", (mail or portal)["browser"], budget, state, mail_url)

    async def check_receipt(settle: float = 2.0, reason: str = "", claimed: str = "") -> dict:
        verified, text = await verify_receipt(portal["browser"], portal_url, tender_reference, claimed=claimed,
                                              settle=settle, budget=budget)
        state["receipt_checked"] = True
        if verified:
            state["receipt"] = text
            state["stages"]["receipt_verified"] = True
            _emit("submit_verified", tender=True)
            return {"receipt_verified": True, "receipt_text": text[:400],
                    "note": "The receipt is confirmed on the tender's portal. Stop and finish submitted=true."}
        if reason:
            state["last_blocker"] = reason
            state["blockers"].append(reason)
        return {"receipt_verified": False,
                "note": "No confirmed receipt on this tender's portal yet — do not claim success, and do not "
                        "re-sign while a late confirmation may still land."}

    state["check"] = lambda: check_receipt(reason="")

    def verify_for_finish(settle: float = 1.0, claimed: str = "", reason: str = "") -> Any:
        return check_receipt(settle=settle, reason=reason, claimed=claimed)

    S, N = {"type": "string"}, {"type": "integer"}
    A = {"action": S, "index": N, "selector": S, "value": S}

    def tool(desc: str, props: dict, fn: Any, limit: float = TOOL_TIMEOUT) -> Tool:
        made = Tool(desc, {"type": "object", "properties": props}, fn)
        made.timeout = limit
        return made

    check_receipt_tool = check_receipt
    _verify_fields_bound = _verify_fields_tool(state, portal_side, "portal")
    _request_signing_bound = _request_signing_tool(state, portal_side, budget, check_receipt_tool)
    mail_reads: list = []  # monotonic stamps of genuine mail observations (freshness proof, no content)

    def _mail_observe() -> Any:
        mail_reads.append(time.monotonic())
        return mail_side.observe()

    _current_code_bound = _current_code_sent_tool(state, budget, mail_reads)

    def _rows_limited(kind: str | None = None, contains: str | None = None) -> list[dict]:
        try:
            rows = store.snapshot(kind=kind, contains=contains, limit=60) if store is not None else []
        except (AttributeError, TypeError, ValueError):
            rows = _rows(store)
        out = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not row.get("rid"):
                continue
            out.append({"rid": str(row["rid"]), "kind": row.get("kind"), "ref": str(row.get("ref") or "")[:80],
                        "title": str(row.get("title") or "")[:120], "revision": str(row.get("revision") or "")[:40],
                        "dated": str(row.get("dated") or "")[:40], "authority": str(row.get("authority") or "")[:60],
                        "site": row.get("site"), "text": str(row.get("text") or "")[:400],
                        "inactive": bool(row.get("invalidated") or row.get("inactive"))})
        return out[:30]

    def _attest_no_bid(reason: str = "", evidence_refs: list | None = None) -> dict:
        records = _active_refs(store)
        refs = [str(r) for r in (evidence_refs or [])]
        if not str(reason or "").strip():
            return {"error": "attest_no_bid needs the concrete reason your review established"}
        if not refs:
            return {"error": "attest_no_bid needs the ACTIVE evidence rid(s) your conclusion rests on"}
        unknown = [ref for ref in refs if ref not in records]
        if unknown:
            return {"error": f"evidence refs not active records in this run's store: {unknown[:4]}"}
        state["attestation"] = {"confirmed": True, "reason": str(reason)[:500], "evidence_refs": refs[:12]}
        return {"attested": True, "refs": refs[:12],
                "note": "Attestation recorded: it lets finish carry the no_bid decision. The semantic call was "
                        "yours; code only checked the rids are active records."}

    async def _crumbs():  # compact progress the model must carry, never page payloads
        return {"stages": [name for name in STAGES if state["stages"].get(name)],
                "blockers": state["blockers"][-2:], "signing_attempts": state["signings"],
                "recoveries": state["recoveries"]}

    def gated(fn: Any) -> Any:
        async def wrapper(**kwargs):
            if budget.remaining() <= 0.0:
                return {"error": "the run budget is spent; finish with the exact stage and blocker you reached"}
            outcome = fn(**kwargs)
            return await outcome if inspect.isawaitable(outcome) else outcome
        return wrapper

    async def progress() -> dict:
        return await _crumbs()

    def tools() -> dict[str, Tool]:
        return {
            "portal_goto": tool("Open a URL you chose on the tender portal.", {"url": S},
                                gated(lambda url: portal_side.call("goto", url))),
            "portal_observe": tool("Re-read the tender portal page (text + indexed elements + alerts).", {},
                                   gated(portal_side.observe)),
            "portal_act": tool("One portal action: click/fill/press/select (index or selector), scroll, back, "
                               "wait, accept_cookies.", A,
                               gated(lambda action=None, index=None, selector=None, value=None:
                                     portal_side.act(action, index, selector, value))),
            "portal_screenshot": tool("The portal viewport as an image when text is not enough.", {},
                                      gated(lambda: portal_side.call("screenshot_b64"))),
            "portal_credentials": tool("The portal's run login, for where its page asks.", {},
                                       gated(lambda: portal_side.call("credentials"))),
            "portal_verify_fields": tool("Report the fields you intended to fill: [{name, expected}] with exact "
                                         "expected values (a checkbox is 'on' when checked). Read-only: matches "
                                         "intent against what the page NOW holds; only matched fields count as "
                                         "verified.",
                                         {"fields": {"type": "array", "items": {"type": "object"}}},
                                         gated(_verify_fields_bound)),
            "mail_goto": tool("Open a URL you chose on the mail site (the signing code arrives there).", {"url": S},
                              gated(lambda url: mail_side.call("goto", url))),
            "mail_observe": tool("Re-read the mail site page.", {}, gated(_mail_observe)),
            "mail_act": tool("One mail-site action (same actions as portal_act).", A,
                             gated(lambda action=None, index=None, selector=None, value=None:
                                   mail_side.act(action, index, selector, value))),
            "mail_credentials": tool("The mail site's run login.", {},
                                     gated(lambda: mail_side.call("credentials"))),
            "request_signing": tool("Ask the portal to start the signing step (submit/confirm button index you "
                                    "observed) and read its result. The page's own validation text is reported; "
                                    "never assume an expired code.", A,
                                    gated(_request_signing_bound)),
            "current_code_sent": tool("Mark that the mail site was freshly read at THIS signing moment and the "
                                      "CURRENT code entered on the portal (say which mail/date you read; an old "
                                      "code never qualifies).", {"source": S}, gated(_current_code_bound)),
            "check_receipt": tool("Whether THIS tender's receipt is confirmed on the portal page (fresh, same "
                                  "origin, carries the tender reference). Call before any re-sign.", {},
                                  gated(lambda: check_receipt_tool(reason=""))),
            "progress": tool("Stages, first blockers and signing attempts so far (counts, no payloads).", {},
                             gated(progress)),
            "review_evidence": tool("The run's recorded evidence (bounded): {rid,kind,ref,title,revision,dated,"
                                    "authority,text}. Inspect it before a no_bid decision or an omitted "
                                    "position.", {"kind": S, "contains": S},
                                    gated(lambda kind=None, contains=None: _rows_limited(kind, contains))),
            "attest_no_bid": tool("Attest confirmed_no_active_match from YOUR review: {reason, evidence_refs} "
                                  "where evidence_refs are ACTIVE rids you just inspected. Code checks only that "
                                  "the refs exist and are active; the semantic call is yours.",
                                  {"reason": S, "evidence_refs": {"type": "array", "items": S}},
                                  gated(lambda reason=None, evidence_refs=None:
                                        _attest_no_bid(reason, evidence_refs))),
            "finish": Tool('finish(result_json) = {"submitted": bool, "receipt": "<observed receipt text>", '
                           '"reason": "<stage + blocker when not submitted>"}.',
                           {"type": "object", "properties": {"result_json": S}},
                           gated(_finish_handler(state, no_bid_open, no_bid_supported, verify_for_finish))),
        }

    # ---- the model loop: one normal attempt, then at most MAX_SIGN_RECOVERIES bounded recoveries ----------------
    base_system = _system(brief, candidate, answer_schema, tender_reference, (ok, problems), partial_rules)
    last_reason = "no receipt observed"
    for attempt in range(1, MAX_SIGN_RECOVERIES + 2):
        if budget.remaining() <= 5.0:
            return RunOutcome(submitted=False, candidate=candidate,
                              reason=f"budget exhausted before submission ({_stage_note(state)})"[:400])
        # a fresh receipt check BEFORE anything else: a late landing is never re-signed over
        state["finish"] = None
        checked = await check_receipt(settle=1.0 if attempt > 1 else 0.0)
        if state.get("receipt"):
            return RunOutcome(submitted=True, receipt=str(state["receipt"])[:2000], candidate=candidate)
        system = base_system if attempt == 1 else f"{base_system}\n\n{_stage_note(state)}"
        agent = QwenAgent(f"finalizer:{attempt}", system, tools(), budget=budget, thinking=attempt > 1,
                          max_output_tokens=4096)
        prompt = (f"Enter and sign this bid on the tender portal at {portal_url}. Attempt {attempt} of "
                  f"{MAX_SIGN_RECOVERIES + 1}." + ("" if attempt == 1 else
                  " Recover from the blockers above: read the page's actual validation text, never reuse an old "
                  "code, and check_receipt before any re-sign."))
        try:
            result = await asyncio.wait_for(agent.run(prompt, max_steps=40),
                                            timeout=max(20.0, min(420.0, budget.timeout(420.0))))
        except TimeoutError:
            result = {"reason": "attempt slice expired before finish"}
            _emit("submit_attempt_stopped", attempt=attempt, error="TimeoutError")
        except BaseException as exc:  # noqa: BLE001 - a model/loop stop must never kill the run: log, recover
            result = {"reason": f"attempt failed ({type(exc).__name__})"}
            _emit("submit_attempt_error", attempt=attempt, error=type(exc).__name__)
        finish_payload = state.get("finish") or (result if isinstance(result, dict) else {})
        if state["receipt_checked"]:  # the check already ran inside finish/check_receipt on this attempt
            if state.get("receipt"):
                return RunOutcome(submitted=True, receipt=str(state["receipt"])[:2000], candidate=candidate)
        elif bool(finish_payload.get("submitted")) and str(finish_payload.get("receipt") or "").strip():
            verified, text = await verify_receipt(portal["browser"], portal_url, tender_reference,
                                                  claimed=str(finish_payload.get("receipt")), settle=2.0,
                                                  budget=budget)
            if verified:
                state["stages"]["receipt_verified"] = True
                return RunOutcome(submitted=True, receipt=text[:2000], candidate=candidate)
            last_reason = "claimed submission not confirmed by a fresh tender-bound portal receipt"
        else:
            last_reason = str(finish_payload.get("reason") or state.get("last_blocker")
                              or "no receipt observed")[:200]
        if state["last_blocker"] and state["last_blocker"] not in state["blockers"]:
            state["blockers"].append(state["last_blocker"])
        state["recoveries"] = attempt
        _emit("submit_attempt_failed", attempt=attempt,
              stages=len([name for name in STAGES if state["stages"].get(name)]))
    reached = [name for name in STAGES if state["stages"].get(name)]
    reason = (f"site submission blocked after {MAX_SIGN_RECOVERIES + 1} attempts at stage "
              f"{reached[-1] if reached else 'start'}: {last_reason}")
    return RunOutcome(submitted=False, candidate=candidate, reason=reason[:400])


# ---- signing tools (defined after finalize_site; the tools dict resolves them at call time) ---------------------


def _verify_fields_tool(state: dict, portal: _Side, name: str):
    async def verify(fields: list | None = None) -> dict:
        observed = await portal.observe()
        proof = {str(item.get("name") or ""): item for item in (observed.get("field_proof") or [])
                 if isinstance(item, dict)}
        wanted = fields if isinstance(fields, list) else []
        state["expected"] = {str(w.get("name") or ""): str(w.get("expected", "")) for w in wanted
                             if isinstance(w, dict)}
        checked, matched, details = 0, 0, []
        for item in wanted:
            if not isinstance(item, dict) or not str(item.get("name") or ""):
                continue
            field = str(item["name"])
            expected = str(item.get("expected", ""))
            entry = proof.get(field)
            if entry is None:
                details.append({"name": field, "matched": False, "why": "no such field on the current page"})
                continue
            checked += 1
            actual = "" if not isinstance(entry, dict) else str(entry.get("value", ""))
            if actual == expected:  # exact match: a checkbox counts as 'on' only when actually checked
                matched += 1
                details.append({"name": field, "matched": True})
            else:
                details.append({"name": field, "matched": False, "expected": expected[:80],
                                "why": "the page holds a different value"})
        ok = bool(checked) and matched == checked
        if ok:
            state["stages"]["form_populated"] = True
            state["stages"]["fields_verified"] = True
            _emit("submit_stage", stage="fields_verified", matched=matched)
        return {"checked": checked, "matched": matched, "verified": ok, "fields": details[:20],
                "alerts": observed.get("alerts", [])[:6], "invalid": observed.get("dialogs", [])[:2],
                "note": "verified means each field's ACTUAL page value equals your expected value" if ok
                        else "fix the unmatched fields, then verify again"}
    return verify


def _request_signing_tool(state: dict, portal: _Side, budget: RuntimeBudget, check):
    async def request_signing(action: str = "click", index: int | None = None, selector: str | None = None,
                              value: str | None = None) -> dict:
        if state["signings"] >= MAX_SIGN_RECOVERIES + 1:
            return {"error": f"the maximum of {MAX_SIGN_RECOVERIES + 1} signing attempts is spent; "
                             "finish with the exact stage and blocker"}
        state["signings"] += 1
        state["stages"]["signing_requested"] = True
        result = await portal.act(action or "click", index, selector, value)
        state["stages"]["signing_attempted"] = True
        _emit("submit_stage", stage="signing_attempted", attempt=state["signings"])
        out = dict(result) if isinstance(result, dict) else {"result": str(result)[:2000]}
        if out.get("error"):
            state["last_blocker"] = f"signing action failed: {out['error']}"[:200]
            state["blockers"].append(state["last_blocker"])
            out.setdefault("note", "the page reported this error — read it, fix what it names")
            return out
        # a late confirmation may already be showing: the receipt check precedes any sign retry
        state["finish"] = None
        checked = await check(reason="signing did not show a receipt yet")
        if checked.get("receipt_verified"):
            return {"receipt_verified": True, "signing_attempt": state["signings"],
                    "note": "This signing landed with a verified receipt — finish submitted=true now."}
        diagnosis = await portal.observe()
        block = "; ".join(str(a)[:160] for a in (diagnosis.get("alerts") or [])[:4]) or \
            (str(diagnosis.get("text") or "")[:300] if diagnosis.get("error") else "no page validation text")
        state["last_blocker"] = f"signing attempt {state['signings']}: {block}"[:200]
        state["blockers"].append(state["last_blocker"])
        out["receipt_verified"] = False
        out["validation"] = {"alerts": diagnosis.get("alerts", [])[:6], "dialogs": diagnosis.get("dialogs", [])[:2],
                             "invalid_fields": [f for f in (diagnosis.get("field_proof") or [])
                                                if isinstance(f, dict) and f.get("invalid")][:8]}
        out["note"] = ("The page's own validation/error text is above. A rejection is NOT automatically an "
                       "expired code: fix what the text names; refresh the mail code ONLY when the page says the "
                       "code is invalid or expired, then current_code_sent and sign again.")
        return out
    return request_signing


def _current_code_sent_tool(state: dict, budget: RuntimeBudget, mail_reads: list):
    async def current_code_sent(source: str = "") -> dict:
        # fresh means: the mail page was actually observed during THIS signing moment (a real read, recorded),
        # and the source says which mail it came from; an old code never qualifies
        fresh = bool(mail_reads) and (time.monotonic() - mail_reads[-1]) <= 120.0
        if not str(source or "").strip():
            return {"error": "state the mail you just read (subject/date) as source"}
        if not fresh:
            return {"error": "the mail site was not freshly read in this signing moment: mail_observe the "
                             "current code first — an old code never qualifies"}
        state["stages"]["current_code_retrieved"] = True
        _emit("submit_stage", stage="current_code_retrieved")
        return {"staged": True, "note": "current code marked; enter it on the portal and request_signing"}
    return current_code_sent
