"""Model provider interface.

A model is named by a spec string "<provider>:<model id>", e.g.
"anthropic:claude-sonnet-5", "openai:gpt-6-sol", "gemini:gemini-3.1-pro-preview", "fake:a".

Real providers live in discovery/providers.py and are imported lazily so tests never need keys.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class LLMError(Exception):
    """Provider failure after retries (network, auth, rate limit, bad JSON)."""


class CostCapExceeded(Exception):
    """The per-task cost cap was reached."""


@dataclass
class CostTracker:
    cap_usd: float
    spent_usd: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, usd: float) -> None:
        """Record spend; thread-safe because extraction and verdicts call providers from worker threads."""
        with self._lock:
            self.spent_usd += usd
            spent = self.spent_usd
        if spent > self.cap_usd:
            raise CostCapExceeded(f"spent ${spent:.2f} > cap ${self.cap_usd:.2f}")


class LLM(Protocol):
    spec: str

    def complete_json(self, *, system: str, prompt: str, schema: dict, max_tokens: int = 8000) -> dict:
        """Return a dict matching `schema` (JSON Schema). Raises LLMError."""
        ...

    def complete_text(self, *, system: str, prompt: str, max_tokens: int = 4000) -> str:
        """Return plain text. Raises LLMError."""
        ...


Responder = Callable[[str, str, dict | None], Any]   # (system, prompt, schema|None) -> dict | str | Exception


@dataclass
class FakeLLM:
    """Test double. `responder` decides the reply from the prompt; raise or return an Exception to fail."""
    spec: str
    responder: Responder
    calls: list[dict] = field(default_factory=list)

    def _reply(self, system: str, prompt: str, schema: dict | None):
        self.calls.append({"system": system, "prompt": prompt, "schema": schema})
        out = self.responder(system, prompt, schema)
        if isinstance(out, Exception):
            raise out
        return out

    def complete_json(self, *, system: str, prompt: str, schema: dict, max_tokens: int = 8000) -> dict:
        out = self._reply(system, prompt, schema)
        if isinstance(out, str):
            out = json.loads(out)
        return out

    def complete_text(self, *, system: str, prompt: str, max_tokens: int = 4000) -> str:
        out = self._reply(system, prompt, None)
        return out if isinstance(out, str) else json.dumps(out)


class LLMFactory:
    """Resolves specs to LLM instances. Tests register fakes; production builds real providers."""

    def __init__(self, cost: CostTracker | None = None):
        self._fakes: dict[str, LLM] = {}
        self._cache: dict[tuple, LLM] = {}
        self._lock = threading.Lock()
        self.cost = cost

    def register(self, llm: LLM) -> None:
        self._fakes[llm.spec] = llm

    def __call__(self, spec: str, *, timeout: float | None = None,
                 delays: tuple[float, ...] | None = None) -> LLM:
        """The model for `spec`. `timeout` (seconds per request) and `delays` (the waits before each retry;
        () for none) tune one use of a real provider, such as ask's calls under a time limit; left out,
        the provider defaults apply (discovery.providers.build). Registered fakes ignore both."""
        if spec in self._fakes:
            return self._fakes[spec]
        opts: dict[str, Any] = {}
        if timeout is not None:
            opts["timeout"] = timeout
        if delays is not None:
            opts["delays"] = tuple(delays)
        key = (spec, opts.get("timeout"), opts.get("delays"))
        with self._lock:
            if key not in self._cache:
                from discovery import providers  # lazy: needs provider SDKs and keys
                self._cache[key] = providers.build(spec, cost=self.cost, **opts)
            return self._cache[key]
