"""Real model providers behind the `LLM` protocol in nua/llm.py.

A spec is "<provider>:<model id>". Keys come from the environment:
- anthropic: ANTHROPIC_API_KEY
- openai: OPENAI_API_KEY
- gemini: GEMINI_API_KEY (the Gemini Developer API, never Vertex)

Each provider enforces JSON output with its SDK's structured-output feature:
- Anthropic: `output_config.format` with a JSON schema (Messages API).
- OpenAI: the Responses API with a strict `json_schema` text format.
- Gemini: `response_mime_type="application/json"` with `response_json_schema`.

A failed call is retried up to 3 times with backoff, then raised as LLMError. A retry is skipped when
another attempt could run past MAX_CALL_SECONDS, so a provider that hangs until the request timeout gets
two attempts, not four, and one model call stays well inside the task budget (nua/runner.py). One use can
ask for a shorter request timeout and fewer retries (`build(..., timeout=, delays=)`, reached through
`LLMFactory`); nua/ask.py does under a time limit. Cost is estimated from each reply's token usage and
charged to the task's CostTracker, which raises CostCapExceeded past its cap.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from nua.llm import LLM, CostCapExceeded, CostTracker, LLMError

KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}
RETRY_DELAYS = (2.0, 8.0, 30.0)          # seconds before retry 1, 2 and 3
REQUEST_TIMEOUT_SECONDS = 300
CALL_SLACK_SECONDS = 60                  # on top of two timed-out attempts, for backoff and slow replies


def call_cap(timeout: float) -> float:
    """Longest one model call may run with this request timeout: two timed-out attempts plus slack."""
    return 2 * timeout + CALL_SLACK_SECONDS


MAX_CALL_SECONDS = call_cap(REQUEST_TIMEOUT_SECONDS)   # worst case of one call at the default timeout
THINKING_HEADROOM = 16_000               # extra output tokens for models that reason before answering


# ---------- prices ----------

@dataclass(frozen=True)
class Price:
    """USD per million tokens. Thinking tokens are billed as output."""
    input: float
    output: float
    long_above: int | None = None        # input tokens above which the long-context rates apply
    long_input: float | None = None
    long_output: float | None = None


# Matched by model id prefix, longest prefix wins.
# Re-check against each provider's pricing page when a model id changes.
PRICES: dict[str, Price] = {
    "claude-fable-5": Price(10.0, 50.0),
    "claude-opus-5": Price(4.0, 20.0),
    "claude-sonnet-5": Price(2.0, 10.0),
    "claude-haiku-4-5": Price(1.0, 5.0),
    "gpt-6-sol": Price(2.0, 10.0, long_above=272_000, long_input=4.0, long_output=15.0),
    "gpt-6-luna": Price(0.10, 0.50),
    "gemini-3.1-pro": Price(2.0, 12.0, long_above=200_000, long_input=4.0, long_output=18.0),
    "gemini-3.8-flash": Price(1.50, 7.50),           # the 2027 price; the 2026 promo is lower
    "gemini-3.1-flash-lite": Price(0.25, 1.50),
}
DEFAULT_PRICE = Price(10.0, 50.0)       # unknown model: priced like the most expensive one above


def price_for(model: str) -> Price:
    """The price entry whose key is the longest prefix of `model`, else DEFAULT_PRICE."""
    best = max((p for p in PRICES if model.startswith(p)), key=len, default=None)
    return PRICES[best] if best else DEFAULT_PRICE


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimated USD for one request, using the long-context rates when the input is above the step."""
    p = price_for(model)
    rate_in, rate_out = p.input, p.output
    if p.long_above is not None and input_tokens > p.long_above:
        rate_in, rate_out = p.long_input or rate_in, p.long_output or rate_out
    return (input_tokens * rate_in + output_tokens * rate_out) / 1_000_000


# ---------- shared plumbing ----------

_COST_LOCK = threading.Lock()   # extraction and verdicts call providers from worker threads


class _BadOutput(Exception):
    """The model replied, but the reply is unusable (empty, truncated, not JSON). Retried."""


class _Refused(Exception):
    """The model or provider refused the request. Not retried."""


def _status(exc: BaseException) -> int | None:
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, _Refused):
        return False
    if isinstance(exc, _BadOutput):
        return True
    if isinstance(exc, (TypeError, AttributeError, NameError, ImportError, ValueError, KeyError)):
        return False                      # a bug, not a provider hiccup (bad JSON is already _BadOutput)
    status = _status(exc)
    if status is None:
        return True                       # connection reset, timeout
    return status in (408, 409, 429) or status >= 500


def _parse_json(text: str | None, schema: dict) -> dict:
    """Parse a JSON object reply and check the schema's top-level required keys."""
    s = (text or "").strip()
    if not s:
        raise _BadOutput("empty reply")
    if s.startswith("```"):
        s = re.sub(r"\A```[A-Za-z]*\s*", "", s)
        s = re.sub(r"\s*```\Z", "", s)
    try:
        data = json.loads(s)
    except json.JSONDecodeError as e:
        raise _BadOutput(f"reply is not JSON ({e})") from None
    if not isinstance(data, dict):
        raise _BadOutput("reply is not a JSON object")
    missing = [k for k in schema.get("required", []) if k not in data]
    if missing:
        raise _BadOutput(f"reply is missing {missing}")
    return data


class _Provider:
    """Retries, cost charging and JSON parsing shared by the three providers."""

    provider = ""
    thinking_headroom = 0

    def __init__(self, spec: str, *, client: Any, cost: CostTracker | None = None,
                 sleep: Callable[[float], None] = time.sleep, delays: tuple[float, ...] = RETRY_DELAYS,
                 clock: Callable[[], float] = time.monotonic, max_seconds: float = MAX_CALL_SECONDS,
                 attempt_seconds: float = REQUEST_TIMEOUT_SECONDS):
        self.spec = spec
        self.model = spec.split(":", 1)[1]
        self.client = client
        self.cost = cost
        self.sleep = sleep
        self.delays = delays
        self.clock = clock
        self.max_seconds = max_seconds            # no retry starts that could end past this
        self.attempt_seconds = attempt_seconds    # the longest one attempt can take (the client timeout)

    def complete_json(self, *, system: str, prompt: str, schema: dict, max_tokens: int = 8000) -> dict:
        """Return a dict matching `schema`, enforced by the provider's structured output. Raises LLMError."""
        return self._with_retries(lambda: _parse_json(self._call(system, prompt, schema, max_tokens), schema))

    def complete_text(self, *, system: str, prompt: str, max_tokens: int = 4000) -> str:
        """Return plain text. Raises LLMError."""
        def once() -> str:
            text = self._call(system, prompt, None, max_tokens)
            if not text.strip():
                raise _BadOutput("empty reply")
            return text
        return self._with_retries(once)

    def _call(self, system: str, prompt: str, schema: dict | None, max_tokens: int) -> str:
        raise NotImplementedError

    def _charge(self, input_tokens: int | None, output_tokens: int | None) -> None:
        if self.cost is None:
            return
        usd = estimate_cost(self.model, input_tokens or 0, output_tokens or 0)
        with _COST_LOCK:
            self.cost.add(usd)

    def _with_retries(self, once: Callable[[], Any]) -> Any:
        started = self.clock()
        for attempt in range(len(self.delays) + 1):
            try:
                return once()
            except CostCapExceeded:
                raise
            except Exception as exc:
                if attempt >= len(self.delays) or not _retryable(exc):
                    raise LLMError(f"{self.spec}: {type(exc).__name__}: {exc}") from exc
                delay = self.delays[attempt]
                if self.clock() - started + delay + self.attempt_seconds > self.max_seconds:
                    raise LLMError(f"{self.spec}: {type(exc).__name__}: {exc} (not retried: another attempt "
                                   f"could run past {self.max_seconds:g}s)") from exc
                self.sleep(delay)
        raise AssertionError("unreachable")


# ---------- providers ----------

class AnthropicLLM(_Provider):
    """Claude through the Messages API. JSON via `output_config.format` (structured outputs)."""

    provider = "anthropic"

    def _call(self, system: str, prompt: str, schema: dict | None, max_tokens: int) -> str:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        if schema is not None:
            import anthropic
            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)}}
        resp = self.client.messages.create(**kwargs)
        usage = resp.usage
        cached = (getattr(usage, "cache_creation_input_tokens", None) or 0) + (getattr(usage, "cache_read_input_tokens", None) or 0)
        self._charge(usage.input_tokens + cached, usage.output_tokens)
        if resp.stop_reason == "refusal":
            raise _Refused("model refused")
        if resp.stop_reason == "max_tokens":
            raise _BadOutput(f"reply cut off at max_tokens={max_tokens}")
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


class OpenAILLM(_Provider):
    """OpenAI through the Responses API. JSON via a strict `json_schema` text format."""

    provider = "openai"
    thinking_headroom = THINKING_HEADROOM

    def _call(self, system: str, prompt: str, schema: dict | None, max_tokens: int) -> str:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "instructions": system,
            "input": prompt,
            "max_output_tokens": max_tokens + self.thinking_headroom,
            "store": False,
        }
        if schema is not None:
            kwargs["text"] = {"format": {"type": "json_schema", "name": "output", "schema": schema, "strict": True}}
        resp = self.client.responses.create(**kwargs)
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self._charge(usage.input_tokens, usage.output_tokens)
        if resp.status == "incomplete":
            reason = getattr(resp.incomplete_details, "reason", None)
            raise _BadOutput(f"reply incomplete ({reason})")
        if resp.status == "failed" or getattr(resp, "error", None):
            raise _BadOutput(f"response failed ({getattr(resp, 'error', None)})")
        for item in getattr(resp, "output", None) or []:
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) == "refusal":
                    raise _Refused(getattr(part, "refusal", "model refused"))
        return resp.output_text or ""


_GEMINI_BLOCKED = {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"}


class GeminiLLM(_Provider):
    """Gemini through the google-genai Developer API. JSON via `response_json_schema`."""

    provider = "gemini"
    thinking_headroom = THINKING_HEADROOM

    def _call(self, system: str, prompt: str, schema: dict | None, max_tokens: int) -> str:
        from google.genai import types
        cfg: dict[str, Any] = {
            "system_instruction": system,
            "max_output_tokens": max_tokens + self.thinking_headroom,
            # no tools are used; this skips the SDK's function-calling loop and its log noise
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
        }
        if schema is not None:
            cfg["response_mime_type"] = "application/json"
            cfg["response_json_schema"] = schema
        resp = self.client.models.generate_content(model=self.model, contents=prompt,
                                                   config=types.GenerateContentConfig(**cfg))
        um = getattr(resp, "usage_metadata", None)
        if um is not None:
            self._charge(um.prompt_token_count, (um.candidates_token_count or 0) + (um.thoughts_token_count or 0))
        feedback = getattr(resp, "prompt_feedback", None)
        if feedback is not None and getattr(feedback, "block_reason", None):
            raise _Refused(f"prompt blocked ({feedback.block_reason})")
        candidates = getattr(resp, "candidates", None) or []
        finish = getattr(candidates[0].finish_reason, "name", str(candidates[0].finish_reason)) if candidates else None
        if finish == "MAX_TOKENS":
            raise _BadOutput(f"reply cut off at max_output_tokens={cfg['max_output_tokens']}")
        if finish in _GEMINI_BLOCKED:
            raise _Refused(f"reply blocked ({finish})")
        return resp.text or ""


# ---------- factory ----------

def build(spec: str, *, cost: CostTracker | None = None, timeout: float | None = None,
          delays: tuple[float, ...] | None = None) -> LLM:
    """Build the real provider for "<provider>:<model id>".

    `timeout` is the request timeout in seconds (default REQUEST_TIMEOUT_SECONDS); one call then stops
    retrying past `call_cap(timeout)`. `delays` are the waits before each retry (default RETRY_DELAYS;
    () means no retry). Raises ValueError for a malformed spec, an unknown provider or a timeout that is
    not positive, and LLMError when the API key is not set (so a missing key degrades like a provider
    outage).
    """
    provider, sep, model = spec.partition(":")
    if not sep or not model:
        raise ValueError(f"bad model spec {spec!r}; expected '<provider>:<model id>'")
    if provider not in KEY_ENV:
        raise ValueError(f"unknown provider {provider!r} in {spec!r}; expected one of {sorted(KEY_ENV)}")
    seconds = float(REQUEST_TIMEOUT_SECONDS if timeout is None else timeout)
    if seconds <= 0:
        raise ValueError(f"timeout must be positive, got {timeout!r}")
    waits = RETRY_DELAYS if delays is None else tuple(float(d) for d in delays)
    from nua.settings import env     # "placeholder" (push-secrets.sh) counts as unset
    key = (env(KEY_ENV[provider]) or "").strip()
    if not key:
        raise LLMError(f"{KEY_ENV[provider]} is not set; {spec} needs it")
    opts: dict[str, Any] = {"cost": cost, "delays": waits, "attempt_seconds": seconds,
                            "max_seconds": call_cap(seconds)}

    if provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=seconds)
        return AnthropicLLM(spec, client=client, **opts)
    if provider == "openai":
        import openai
        client = openai.OpenAI(api_key=key, max_retries=0, timeout=seconds)
        return OpenAILLM(spec, client=client, **opts)
    from google import genai
    from google.genai import types
    # enterprise=False pins the Developer API even if GOOGLE_GENAI_USE_VERTEXAI is set in the environment.
    client = genai.Client(enterprise=False, api_key=key,
                          http_options=types.HttpOptions(timeout=int(seconds * 1000)))
    return GeminiLLM(spec, client=client, **opts)
