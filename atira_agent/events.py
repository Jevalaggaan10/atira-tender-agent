"""A tiny redacted event log: event name, phase, counts, tokens and error classes only.

Never a payload, a page text, a credential, a TAN or a scenario value: a run's log ends up on the run page, and the
package must stay code-only. `runtime.py` imports this module at call time and skips logging when it is missing, so
tests can run without it. Set ATIRA_EVENTS_FILE to write JSON lines somewhere; by default nothing is written (stdout
gets the one-line summary the operator needs). Values are stringified, truncated and passed through the secret
redactor below as a second line of defence.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any

MAX_FIELD = 160
MAX_KEYS = 12

# generic shapes that must never leave the process, whatever the field is called
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(authorization|bearer|x-api-key|api[_-]?key|password|passwort|token|secret)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(TAN|Einmalpasswort|Einmal-PIN|einmalpin)\b\s*[:=]?\s*\S*"),
    re.compile(r"\b(?:\d[\d -]{2,17}\d)\b"),  # digits run together: a TAN, a TAN-protected total, an account
)
SECRET_KEY_WORDS = ("password", "passwort", "secret", "credential", "apikey", "api_key", "cookie",
                    "authorization")
# "token" on its own is a credential (a run token); "<something>_tokens" is a counter, and only a number survives
CREDENTIAL_WORDS = ("token",)


def _is_credential_key(key: str) -> bool:
    lowered = key.lower().replace("_", "").replace("-", "")
    return _token_count_key(key) or any(word in lowered for word in SECRET_KEY_WORDS + CREDENTIAL_WORDS)
# a *token* key is only safe when it is a plain numeric counter (prompt_tokens, completion_tokens); a free-text or
# credential-shaped value under such a name is redacted, so a log line can never carry a run token or a TAN
TOKEN_COUNT_KEYS = ("prompttokens", "completiontokens", "totaltokens", "cachedtokens", "reasoningtokens", "tokens")


def _token_count_key(key: str) -> bool:
    return key.lower().replace("_", "").replace("-", "") in TOKEN_COUNT_KEYS


def _redact_text(value: str) -> str:
    text = value.replace("\n", " ")
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text[:MAX_FIELD] + "…" if len(text) > MAX_FIELD else text


def _safe_token_count(value: Any) -> Any:
    return value if isinstance(value, int) and not isinstance(value, bool) else (
        int(value) if isinstance(value, float) and float(value).is_integer() else "[redacted]")


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):  # a bare number is a count, never text: keep it exact
        return round(value, 3) if isinstance(value, float) else value
    if isinstance(value, (list, tuple, set)):
        items = list(value)[:MAX_KEYS]
        return [_scalar(item) for item in items]
    if isinstance(value, dict):
        keys = sorted(value)[:MAX_KEYS]
        return {str(key): (_safe_token_count(value[key]) if _token_count_key(str(key))
                           else "[redacted]" if _is_credential_key(str(key)) else _scalar(value[key]))
                for key in keys}
    return _redact_text(str(value))


def sanitize(fields: dict[str, Any]) -> dict[str, Any]:
    """Keep only redacted, bounded scalars out of arbitrary event fields."""
    clean: dict[str, Any] = {}
    for key, value in (fields or {}).items():
        if value is None:
            continue
        name = str(key)
        if _token_count_key(name):
            # only a plain number survives a *_tokens field; a string there could be a credential
            clean[name] = _safe_token_count(value)
        elif _is_credential_key(name):
            clean[name] = "[redacted]"
        else:
            clean[name] = _scalar(value)
    return clean


def _error_class(value: Any) -> Any:
    """Errors arrive as exceptions or names: log the class, never the message (it can carry a page or a token)."""
    if isinstance(value, BaseException):
        return type(value).__name__
    if isinstance(value, (list, tuple)):
        return [_error_class(item) for item in value]
    if isinstance(value, str):
        return value.split(":", 1)[0].strip()[:80]
    return value


def emit(event: str, *, phase: str | None = None, **fields: Any) -> None:
    """One line per event, time on the process clock; never raises, logging must not break a run."""
    try:
        record: dict[str, Any] = {"t": round(time.monotonic(), 3)}
        record["event"] = _redact_text(str(event))
        if phase:
            record["phase"] = _redact_text(str(phase))
        if "error" in fields:
            fields = {**fields, "error": _error_class(fields["error"])}
        if "errors" in fields:
            fields = {**fields, "errors": _error_class(fields["errors"])}
        record.update(sanitize(fields))
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        if path := os.environ.get("ATIRA_EVENTS_FILE"):
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        else:
            print(f"[events] {line}", file=sys.stderr, flush=True)
    except Exception:  # pragma: no cover - the log must never be the reason a run fails
        return
