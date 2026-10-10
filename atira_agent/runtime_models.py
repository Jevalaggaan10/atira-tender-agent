"""Hosted-Qwen tool-call loop: one bounded conversation with tools and a terminal `finish`.

Fair-play: this module only dispatches model calls. Every navigation target, search, document choice and decision
comes back as a model tool-call; nothing here chooses anything on its own. Every request goes to the proxy at
`QWEN_BASE_URL`/`QWEN_API_KEY` with `models.QWEN_MODEL`, thinking off by default (the measured speed repair: the
proxy accepts `extra_body={"chat_template_kwargs":{"enable_thinking":<bool>}}`).
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import os
import random
from typing import Any, Awaitable, Callable

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from .contracts import RuntimeBudget

TOOL_RESULT_LIMIT = 30_000  # chars; longer tool results are tail-truncated with a marker
CONTEXT_CHAR_SOFT_CAP = 220_000  # rough conversation size where trimming starts
CONTEXT_CHAR_TRIM_TO = 150_000  # trim oldest tool results down to this size
KEEP_RECENT_MESSAGES = 8  # never trim away the most recent exchange
TRIM_MARKER = "[...older tool result trimmed to keep the conversation within the model's context...]"
MAX_ATTEMPTS = 4  # per request: 1 try + up to 3 retries, thinking stays bounded
RETRY_BASE_DELAY = 4.0  # seconds; doubles per attempt (4, 8, 16), capped by the budget's remaining time


class ModelError(RuntimeError):
    """A model call that will not succeed as asked (non-retryable status, or the budget ran out)."""


def _emit(event: str, **fields: Any) -> None:
    """Log through the parent's events module if it exists; never carries payloads or secrets."""
    try:
        # look the module up by name: it may be a stand-in installed by a test, and a stale `from . import`
        # would hide it
        module = sys.modules.get("atira_agent.events") or sys.modules.get("atira_agent.events")
        if module is None:
            from . import events as module  # parent-owned, provided at run time; imported lazily
        events = module
    except Exception:  # pragma: no cover - only before the parent adds events.py
        return
    try:
        events.emit(event, **fields)
    except Exception:  # pragma: no cover - logging must never break a run
        pass


def _proxy():
    import models  # top-level kit module (parent-owned); imported at call time so tests can patch QWEN_MODEL

    return models.QWEN_MODEL


def _max_concurrent_calls() -> int:
    try:
        return max(1, int(os.environ.get("ATIRA_MAX_MODEL_CALLS", "3")))
    except ValueError:
        return 3


_SEMS: list = []  # [loop, limit, semaphore]: the shared model-call queue while on the same loop and limit


def _call_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    limit = _max_concurrent_calls()
    if _SEMS and _SEMS[0] is loop and _SEMS[1] == limit:
        return _SEMS[2]
    semaphore = asyncio.Semaphore(limit)
    _SEMS[:] = [loop, limit, semaphore]
    return semaphore


_CLIENT: list = []  # [loop, env_key, client]: one pooled client per event loop while the env is unchanged


def _client(timeout_s: float) -> AsyncOpenAI:
    """The hosted-Qwen client (env and proxy path exactly as models.qwen()), pooled per event loop.

    `max_retries=0`: retrying is this module's own, budget-bounded logic — a pooled SDK must not retry behind
    our back and eat scripted replies or the clock. When the loop changed (tests, a new run), the stale pooled
    client is closed once so no connection lingers on a closed loop.
    """
    loop = asyncio.get_running_loop()
    base_url, api_key = os.environ["QWEN_BASE_URL"], os.environ["QWEN_API_KEY"]
    stale = None
    if _CLIENT and _CLIENT[0] is loop and _CLIENT[1] == (base_url, api_key):
        client = _CLIENT[2]
    else:
        client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=360, max_retries=0)
        stale = _CLIENT[2] if _CLIENT else None
        _CLIENT[:] = [loop, (base_url, api_key), client]
    if stale is not None:
        try:
            stale._state.close()  # release the previous loop's connections without awaiting on a closed loop
        except Exception:
            pass
    return client.with_options(timeout=max(timeout_s, 5.0))


@dataclasses.dataclass
class Tool:
    """One tool: a description, a JSON-schema `params`, and an async fn(**kwargs) -> str | dict | list."""

    description: str
    params: dict
    fn: Callable[..., Awaitable[Any] | Any]


def _to_chatml_tool(name: str, tool: Tool) -> dict:
    return {"type": "function", "function": {"name": name, "description": tool.description,
                                             "parameters": tool.params}}


def _truncate(text: str, limit: int = TOOL_RESULT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[truncated: {len(text) - limit} more characters not shown]"


def _stringify(result: Any) -> str:
    limit = TOOL_RESULT_LIMIT  # read at call time so a test (or caller) can lower the cap
    if isinstance(result, str):
        return _truncate(result, limit)
    try:
        return _truncate(json.dumps(result, ensure_ascii=False, default=str), limit)
    except (TypeError, ValueError):
        return _truncate(str(result), limit)


def _conversation_chars(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            total += sum(len(str(part.get("text", ""))) for part in content if isinstance(part, dict))
        total += len(json.dumps(message.get("tool_calls") or "", ensure_ascii=False))
    return total


def _trim(messages: list[dict]) -> list[dict]:
    """Replace the oldest oversized tool results (keeping system, task and the recent tail) with a marker."""
    size = _conversation_chars(messages)
    if size <= CONTEXT_CHAR_SOFT_CAP:
        return messages
    out = list(messages)
    for i in range(2, max(2, len(out) - KEEP_RECENT_MESSAGES)):
        if size <= CONTEXT_CHAR_TRIM_TO:
            break
        message = out[i]
        if message.get("role") != "tool" or message.get("content") == TRIM_MARKER:
            continue
        saved = len(str(message.get("content") or ""))
        out[i] = {**message, "content": TRIM_MARKER}
        size -= max(0, saved - len(TRIM_MARKER))
    return out


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, (APITimeoutError, APIConnectionError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return isinstance(exc, httpx.TransportError)


def _image_from_result(result: Any) -> dict | None:
    """Normalize a tool's vision payload to one image_url content part, or None.

    Accepted shapes: {"image_b64": "<png base64>"} or {"image": {"type": "image_url", "image_url": {...}}}.
    """
    if not isinstance(result, dict):
        return None
    if result.get("image_b64"):
        return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{result['image_b64']}"}}
    image = result.get("image")
    if isinstance(image, dict) and image.get("type") == "image_url" and image.get("image_url"):
        return {"type": "image_url", "image_url": image["image_url"]}
    return None


def _parse_arguments(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _normalize_finish(result: Any) -> dict | None:
    """Decode a finish payload: dict as-is, a JSON string decoded; anything else None (an invalid finish)."""
    if isinstance(result, dict):
        return result
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except ValueError:
        parsed = None
    return parsed if isinstance(parsed, dict) else None


def _thinking_extra_body(thinking: bool, base: dict | None = None) -> dict:
    """Merge (never overwrite) the thinking switch into extra_body."""
    extra = dict(base or {})
    kwargs = dict(extra.get("chat_template_kwargs") or {})
    kwargs["enable_thinking"] = bool(thinking)
    extra["chat_template_kwargs"] = kwargs
    return extra


class QwenAgent:
    """One named agent: system prompt + tools + one bounded conversation. `run` returns the `finish` result."""

    def __init__(self, name: str, system: str, tools: dict[str, Tool], *, budget: RuntimeBudget,
                 max_output_tokens: int = 4096, temperature: float = 0.0, thinking: bool = False,
                 extra_body: dict | None = None):
        self.name, self.system, self.tools = name, system, dict(tools)
        self.budget = budget
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self.thinking = thinking
        self.extra_body = extra_body
        self.last_call: dict = {}
        self.step = 0  # the agent's own step counter, set while `run` drives the loop (0 before it starts)
        self._final: dict | None = None

    async def run(self, task: str, *, max_steps: int = 30, images_first: list[dict] | None = None) -> dict:
        """Drive the loop: model calls tools until it calls `finish(result_json)` or steps/budget run out."""
        self._final = None
        self.step = 0  # the real agent step, visible to _chat so a model_call never logs a misleading zero
        content: Any = task
        if images_first:
            content = [{"type": "text", "text": task}] + list(images_first)
        messages: list[dict] = [{"role": "system", "content": self.system}, {"role": "user", "content": content}]
        tools = dict(self.tools)
        if "finish" not in tools:
            tools["finish"] = Tool("Call this once when done, with the final result as a JSON string.",
                                   {"type": "object", "properties": {"result_json": {"type": "string"}},
                                    "required": ["result_json"]},
                                   lambda result_json="{}": result_json)
        specs = [_to_chatml_tool(name, tool) for name, tool in tools.items()]
        for step in range(1, max_steps + 1):
            self.step = step  # _chat logs THIS step, not the per-request retry attempt
            message = await self._chat(messages, specs)
            calls = message.get("tool_calls") or []
            if not calls:
                text = message.get("content")
                messages.append({"role": "assistant", "content": text})
                return {"summary": text, "finished": False, "note": "the model answered in plain text without finish"}
            messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": calls})
            for call in calls:
                fn = (call.get("function") or {})
                name = fn.get("name") or ""
                args = _parse_arguments(fn.get("arguments"))
                # diagnostic only: tool name and argument COUNTS (never argument values: no payload or secret)
                _emit("tool_call", agent=self.name, step=step, tool=name, arg_count=len(args))
                tool = tools.get(name)
                if tool is None:
                    result = {"error": f"unknown tool {name!r}; available: {sorted(tools)}"}
                else:
                    try:
                        # the tool's own `timeout` attribute wins when it sets one (browser/document tools do);
                        # the loop's budget-bounded default applies otherwise
                        limit = getattr(tool, "timeout", None) or self._tool_limit()
                        result = await asyncio.wait_for(self._invoke(tool, args), timeout=min(limit, self._tool_limit()))
                    except asyncio.TimeoutError:
                        result = {"error": f"tool {name!r} took too long; re-observe and try a smaller step"}
                    except TypeError as exc:
                        # wrong or missing arguments for this tool: the model can repair its own call
                        result = {"error": f"tool {name!r} rejected these arguments: {exc}; check the schema "
                                           "and call it again"}
                        _emit("tool_error", agent=self.name, step=step, tool=name, error="TypeError")
                    except Exception as exc:  # helpers surface their errors to the model, they never crash the loop
                        result = {"error": f"{type(exc).__name__}: {exc}"}
                        _emit("tool_error", agent=self.name, step=step, tool=name, error=type(exc).__name__)
                tool_text = _stringify(result)
                messages.append({"role": "tool", "tool_call_id": call.get("id") or "", "name": name,
                                 "content": tool_text})
                image = _image_from_result(result)  # vision: {"image_b64": "..."} or {"image": {image_url part}}
                if image:
                    messages.append({"role": "user", "content": [{"type": "text", "text": f"Image from tool {name}."},
                                                                 image]})
                if name == "finish":
                    # `finish` is terminal ONLY when the tool returned a usable result. An error result, invalid
                    # JSON, or a wrapper that carries no decodable payload (e.g. result_json missing) is
                    # REPAIRABLE: the error text already went back as the tool message, so record nothing,
                    # return nothing and let the model fix its own call on the next turn. The terminal result is
                    # the decoded result_json (or the custom tool's decoded return) — never raw wrapper args.
                    payload = _normalize_finish(result if not (isinstance(result, dict) and result.get("error"))
                                                else None)
                    if (isinstance(result, dict) and result.get("error")) or payload is None:
                        _emit("finish_invalid", agent=self.name, step=step)
                        continue
                    self._final = payload
                    _emit("agent_finish", agent=self.name, step=step, result_keys=sorted(self._final)[:12])
                    return self._final
            messages = _trim(messages)
            if self.budget.remaining() <= 0:
                break
        _emit("agent_incomplete", agent=self.name, steps=max_steps)
        return {"summary": None, "finished": False, "error": "no finish within the step or budget limit"}

    @staticmethod
    async def _invoke(tool: Tool, args: dict) -> Any:
        outcome = tool.fn(**args)
        return await outcome if asyncio.iscoroutine(outcome) or asyncio.isfuture(outcome) else outcome

    def _tool_limit(self) -> float:
        """A tool call never outlives the shared budget: helpers get what remains, capped."""
        return max(1.0, min(120.0, self.budget.timeout(90)))

    async def _chat(self, messages: list[dict], specs: list[dict]) -> dict:
        model = _proxy()
        extra = _thinking_extra_body(self.thinking, self.extra_body)
        for attempt in range(MAX_ATTEMPTS):
            timeout_s = self.budget.timeout(90)
            if timeout_s <= 0:
                raise ModelError(f"{self.name}: no budget left for a model call")
            try:
                # the queue wait itself is bounded by the budget, and the budget is recomputed under the slot
                semaphore = _call_semaphore()
                acquired = False
                try:
                    await asyncio.wait_for(semaphore.acquire(), timeout=max(0.0, self.budget.timeout(120)))
                    acquired = True
                except asyncio.TimeoutError:
                    raise ModelError(f"{self.name}: no model slot and no budget left to wait for one") from None
                try:
                    # a queue wait can consume the budget: recompute, and give up rather than dispatch stale
                    timeout_s = self.budget.timeout(90)
                    if timeout_s <= 0:
                        raise ModelError(f"{self.name}: the budget ran out while waiting for a model slot")
                    try:
                        # the cancellation deadline is exactly the budget's own slice: a request never runs past
                        # remaining(), with no padding that could let a call outlive the budget
                        reply = await asyncio.wait_for(
                            _client(timeout_s).chat.completions.create(
                                model=model, messages=messages, tools=specs, temperature=self.temperature,
                                max_tokens=self.max_output_tokens, extra_body=extra),
                            timeout=timeout_s)
                    except asyncio.TimeoutError as exc:
                        raise APITimeoutError(request=None) from exc
                finally:
                    if acquired:
                        semaphore.release()
            except ModelError:
                raise  # the budget, not the proxy, said stop
            except Exception as exc:
                if not _retryable(exc) or attempt >= MAX_ATTEMPTS - 1:
                    status = getattr(exc, "status_code", None)
                    _emit("model_error", agent=self.name, retry=attempt, status=status, error=type(exc).__name__)
                    if not _retryable(exc):
                        raise ModelError(f"{self.name}: model call failed ({type(exc).__name__}"
                                         f"{'' if status is None else f' {status}'}): {exc}") from exc
                    raise ModelError(f"{self.name}: model call kept failing after retries "
                                     f"({type(exc).__name__})") from exc
                delay = min(RETRY_BASE_DELAY * (2 ** attempt), self.budget.remaining())
                _emit("model_retry", agent=self.name, retry=attempt, error=type(exc).__name__,
                      wait=round(delay, 2))
                await asyncio.sleep(delay + random.uniform(0, delay * 0.1))
                continue
            usage = getattr(reply, "usage", None)
            self.last_call = {"model": model, "thinking": self.thinking, "attempt": attempt,
                              "prompt_tokens": getattr(usage, "prompt_tokens", None),
                              "completion_tokens": getattr(usage, "completion_tokens", None)}
            _emit("model_call", agent=self.name, step=self.step, retry=attempt,
                  prompt_tokens=self.last_call["prompt_tokens"], completion_tokens=self.last_call["completion_tokens"],
                  thinking=self.thinking)
            choice = reply.choices[0]
            message = choice.message
            return {"content": message.content,
            # replay every assistant tool call exactly as the API shape demands: type:"function" is required,
            # so a dropped type is a 400 from the real hosted model even though permissive local fakes accept it
            "tool_calls": [{"id": c.id, "type": "function",
                            "function": {"name": c.function.name, "arguments": c.function.arguments}}
                           for c in (message.tool_calls or [])]}
        raise ModelError(f"{self.name}: unreachable retry state")  # pragma: no cover


async def decide_typed(state: dict, question: str, options: list[str]) -> str | None:
    """A quick typed decision from hosted Clef, or None on any failure (callers then ask Qwen).

    Never used for pricing, compliance or extraction authority — the model with tools owns those decisions.
    """
    base, key = os.environ.get("CLEF_BASE_URL"), os.environ.get("CLEF_API_KEY")
    if not base or not key or not options:
        return None
    body = {"state": state, "questions": {"q": {"type": "choice", "instructions": question,
                                                "criteria": {option: option for option in options}}}}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(base.rstrip("/") + "/v1/decide", json=body,
                                         headers={"Authorization": f"Bearer {key}"})
            response.raise_for_status()
            answers = response.json().get("answers", {}).get("q") or {}
    except Exception as exc:
        _emit("decide_typed_error", error=type(exc).__name__)
        return None
    chosen = answers.get("choice") or answers.get("value")
    if isinstance(chosen, str):
        if chosen in options:
            return chosen
        for option in options:  # Clef may restate the option
            if option.lower() in chosen.lower():
                return option
    return None
