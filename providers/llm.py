"""The model client, and the one rule that governs it.

Doxa asks models questions on a customer's behalf — "what do you recommend for X?" — and reports what
came back. That output is the product, so two things matter more than anything else about this client.

**A failed call is never a negative answer.** If the router errors, times out or gates a model, the
honest report is "we could not ask", not "you were not mentioned". Silently converting an outage into
"your brand is invisible in AI search" would be the single most damaging bug this service could ship,
because the customer would act on it. Every failure here is recorded and surfaced.

**The model chain is per-model, not per-account.** The 0G router gates availability with a 403
BALANCE_INSUFFICIENT on individual models while others on the same key answer normally. A sibling ASP
returned 500 to an already-charged caller purely because its single configured model was gated. So the
chain is tried in order and the model that actually answered is reported alongside the answer.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from config import Settings, get_settings


class LLMUnavailable(RuntimeError):
    """Every configured model refused or failed. Never convert this into an empty answer."""


@dataclass
class Completion:
    text: str
    model: str
    attempts: list[dict[str, Any]] = field(default_factory=list)
    tee_verified: bool | None = None
    latency_ms: int = 0
    finish_reason: str | None = None

    @property
    def truncated(self) -> bool:
        """The model ran out of budget mid-answer. The API says so; guessing from the text is worse."""
        return self.finish_reason == "length"

    def as_dict(self) -> dict[str, Any]:
        return {"model": self.model, "attempts": self.attempts,
                "tee_verified": self.tee_verified, "latency_ms": self.latency_ms,
                "finish_reason": self.finish_reason}


class LLM:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    @property
    def available(self) -> bool:
        return self.settings.llm_configured

    def models(self) -> list[str]:
        return list(self.settings.llm_model_chain)

    def complete(self, prompt: str, *, system: str = "", temperature: float = 0.0,
                 max_tokens: int = 1200, model: str | None = None,
                 timeout: int = 90, verify_tee: bool = True,
                 deadline_s: float | None = None) -> Completion:
        """Ask one question. Tries the model chain in order; raises if none answered.

        `timeout` bounds a single request; `deadline_s` bounds the whole chain. Both are needed,
        because the fallback multiplies the first: five models at 70 seconds each is 350 seconds, and
        the x402 challenge only promises the caller 300. Once the deadline has passed no further
        model is tried, and the failure says so.
        """
        if not self.available:
            raise LLMUnavailable("No model API key is configured, so no model could be asked.")

        chain = [model] if model else self.models()
        deadline = (time.perf_counter() + deadline_s) if deadline_s else None
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": prompt}]
        attempts: list[dict[str, Any]] = []
        started = time.perf_counter()

        for name in chain:
            if deadline is not None:
                left = deadline - time.perf_counter()
                if left <= 5:
                    attempts.append({"model": name, "error": "skipped: the time budget for this "
                                                             "call was already spent"})
                    break
                request_timeout = int(min(timeout, left))
            else:
                request_timeout = timeout
            body: dict[str, Any] = {"model": name, "messages": messages,
                                    "temperature": temperature, "max_tokens": max_tokens}
            if verify_tee:
                body["verify_tee"] = True
            try:
                r = requests.post(
                    f"{self.settings.llm_base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {self.settings.llm_api_key}",
                             "Content-Type": "application/json"},
                    json=body, timeout=request_timeout)
            except Exception as e:  # noqa: BLE001
                attempts.append({"model": name, "error": f"{type(e).__name__}: {str(e)[:120]}"})
                continue

            if r.status_code != 200:
                attempts.append({"model": name, "status": r.status_code,
                                 "error": r.text[:200]})
                continue
            try:
                data = r.json()
                raw = (data["choices"][0]["message"]["content"] or "").strip()
            except Exception as e:  # noqa: BLE001
                attempts.append({"model": name, "status": r.status_code,
                                 "error": f"unparseable response: {type(e).__name__}"})
                continue

            text = strip_reasoning(raw)
            if not text:
                # Several models on this router emit a <think> block before answering. When the token
                # budget runs out inside it, the response is HTTP 200 carrying only reasoning and no
                # answer. Treating that as a valid completion would publish the model's scratchpad as
                # the customer's result, so it counts as a failed attempt and the chain moves on.
                attempts.append({"model": name, "status": 200,
                                 "error": "the model returned only reasoning, no answer "
                                          "(truncated inside a <think> block)"})
                continue

            finish = (data.get("choices") or [{}])[0].get("finish_reason")
            attempts.append({"model": name, "status": 200, "ok": True,
                             "finish_reason": finish})
            return Completion(text=text, model=name, attempts=attempts,
                              tee_verified=bool(data.get("tee_verified")),
                              finish_reason=finish,
                              latency_ms=int((time.perf_counter() - started) * 1000))

        raise LLMUnavailable(
            "No configured model could answer. This is an outage on our side, not a finding about "
            "your site: " + "; ".join(f"{a['model']}: {a.get('error', 'failed')}" for a in attempts))

    def complete_json(self, prompt: str, *, system: str = "",
                      max_tokens: int = 1200, deadline_s: float | None = None,
                      **kw) -> tuple[Any, Completion]:
        """Ask for JSON and parse it, surviving the two things models reliably do to it.

        They wrap it in fences and in a `<think>` block, which is handled by cleaning. And they run
        out of token budget partway through, which is not: the answer is genuinely incomplete. The
        API reports that as `finish_reason: length`, so the call is retried once with a larger budget
        rather than failing on a truncation we caused by being stingy. Only if a *complete* response
        still will not parse is the salvage path used, and only if that fails do we raise.
        """
        started = time.perf_counter()
        c = self.complete(prompt, system=system or JSON_SYSTEM, max_tokens=max_tokens,
                          deadline_s=deadline_s, **kw)
        try:
            return _parse_json(c.text), c
        except ValueError:
            if not c.truncated:
                # A complete answer that will not parse is the model's error, not a budget problem.
                # Salvage what is structurally valid rather than discarding a paid-for call.
                return _salvage_json(c.text), c

        # The retry shares the original budget rather than getting a fresh one.
        remaining = (deadline_s - (time.perf_counter() - started)) if deadline_s else None
        if remaining is not None and remaining <= 5:
            return _salvage_json(c.text), c
        retry = self.complete(prompt, system=system or JSON_SYSTEM,
                              max_tokens=min(max_tokens * 3, 8000),
                              deadline_s=remaining, **kw)
        try:
            return _parse_json(retry.text), retry
        except ValueError:
            return _salvage_json(retry.text), retry


# Reasoning wrappers used by the models on this router. The closing tag is optional in the pattern so
# that a block truncated by the token limit is still removed rather than returned as the answer.
_REASONING = re.compile(
    r"<(think|thinking|reasoning|scratchpad)\b[^>]*>.*?(?:</\1>|\Z)", re.I | re.S)


def strip_reasoning(text: str) -> str:
    """Remove a model's private reasoning, leaving the answer it actually gave."""
    return _REASONING.sub("", text or "").strip()


def as_object(data: Any, key: str) -> dict[str, Any]:
    """Normalise a model's JSON to the object shape the caller asked for.

    Asked for `{"figures": [...]}`, models regularly return the bare `[...]` instead — and after
    salvaging a truncated response that is even more likely, because the outer object may be the part
    that was cut off. Every caller then did `data.get(...)` on a list and raised AttributeError, which
    surfaced as an engine failure on a call the customer had already paid for. Reshaping here is
    lossless and keeps the shape check in one place.
    """
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return {key: data}
    return {}


JSON_SYSTEM = ("You answer only with valid JSON. No prose before or after, no markdown fences. "
               "If you are unsure of a value, use null rather than inventing one.")

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def _parse_json(text: str) -> Any:
    """Parse a model's JSON, recovering from the wrappers they add anyway."""
    cleaned = _FENCE.sub("", (text or "").strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost balanced object or array in the response.
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"The model did not return usable JSON: {cleaned[:200]!r}")


def _salvage_json(text: str) -> Any:
    """Recover the complete part of a JSON document that was cut off mid-write.

    A truncated `{"figures": [ {...}, {...}, {"label": "half` still contains several whole objects,
    and discarding them means charging for a call and returning nothing. The rule is strict: only
    *complete* elements are kept. Nothing is repaired by inventing a closing value, because a
    half-written figure completed by us would be a fabricated figure.
    """
    cleaned = _FENCE.sub("", strip_reasoning(text or "").strip())
    start = min([i for i in (cleaned.find("{"), cleaned.find("[")) if i != -1], default=-1)
    if start == -1:
        raise ValueError(f"The model returned no JSON at all: {cleaned[:200]!r}")
    body = cleaned[start:]

    # Walk the text keeping the stack of open containers, remembering every position from which the
    # document could be validly closed. Two kinds of position qualify:
    #
    #   * just after a container closes — the end of a whole nested element;
    #   * a comma, because everything before it is a complete value.
    #
    # The comma case is what makes this work on a flat object. `{"question": "...", "answered": true,
    # "audience": "...` has no nested container at all, so an earlier version recovered nothing from
    # it and the whole paid call failed — even though several fields were complete and usable.
    stack: list[str] = []
    in_string = False
    escaped = False
    resume: tuple[int, list[str]] | None = None

    for i, ch in enumerate(body):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack:
                break
            stack.pop()
            if not stack:
                try:
                    return json.loads(body[:i + 1])   # the document was complete after all
                except json.JSONDecodeError:
                    break
            resume = (i, list(stack))
        elif ch == "," and stack:
            # Everything before the comma is a complete value; the comma itself is dropped.
            resume = (i - 1, list(stack))

    if resume is not None:
        end, still_open = resume
        candidate = body[:end + 1] + "".join(reversed(still_open))
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    raise ValueError(f"The model's JSON was cut off and nothing complete could be recovered: "
                     f"{body[:200]!r}")
