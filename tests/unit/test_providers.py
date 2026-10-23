"""Offline tests for nua/providers.py. Every SDK client is a mock; nothing touches the network."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nua import providers
from nua.llm import CostCapExceeded, CostTracker, LLMError

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "note"],
    "properties": {
        "answer": {"type": "integer"},
        "note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
}
GOOD = json.dumps({"answer": 42, "note": None})


class Recorder:
    """A stand-in for one SDK method: records kwargs, replays scripted replies (raise Exceptions)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        out = self.replies.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class StatusError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def no_sleep(delays: list[float]):
    return lambda seconds: delays.append(seconds)


# ---------- response builders ----------

def anthropic_reply(text: str, *, stop: str = "end_turn", tokens=(1000, 200)):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1]))


def openai_reply(text: str, *, status: str = "completed", tokens=(1000, 200)):
    return SimpleNamespace(output_text=text, status=status, error=None,
                           incomplete_details=None if status == "completed" else SimpleNamespace(reason="max_output_tokens"),
                           usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1]))


def gemini_reply(text: str | None, *, finish: str = "STOP", tokens=(1000, 150, 50)):
    return SimpleNamespace(text=text, prompt_feedback=None,
                           candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish))],
                           usage_metadata=SimpleNamespace(prompt_token_count=tokens[0], candidates_token_count=tokens[1],
                                                          thoughts_token_count=tokens[2]))


def anthropic_llm(*replies, cost=None, delays=None):
    create = Recorder(*replies)
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    llm = providers.AnthropicLLM("anthropic:claude-sonnet-5", client=client, cost=cost,
                                 sleep=no_sleep(delays if delays is not None else []))
    return llm, create


def openai_llm(*replies, cost=None, delays=None):
    create = Recorder(*replies)
    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    llm = providers.OpenAILLM("openai:gpt-6-sol", client=client, cost=cost,
                              sleep=no_sleep(delays if delays is not None else []))
    return llm, create


def gemini_llm(*replies, cost=None, delays=None):
    generate = Recorder(*replies)
    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    llm = providers.GeminiLLM("gemini:gemini-3.1-pro-preview", client=client, cost=cost,
                              sleep=no_sleep(delays if delays is not None else []))
    return llm, generate


# ---------- request shapes ----------

def test_anthropic_json_uses_output_config_json_schema():
    llm, create = anthropic_llm(anthropic_reply(GOOD))
    out = llm.complete_json(system="sys", prompt="hello", schema=SCHEMA, max_tokens=1234)
    assert out == {"answer": 42, "note": None}
    kw = create.calls[0]
    assert kw["model"] == "claude-sonnet-5"
    assert kw["system"] == "sys"
    assert kw["max_tokens"] == 1234
    assert kw["messages"] == [{"role": "user", "content": "hello"}]
    fmt = kw["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert fmt["schema"]["required"] == ["answer", "note"]
    assert "tools" not in kw


def test_anthropic_text():
    llm, create = anthropic_llm(anthropic_reply("plain answer"))
    assert llm.complete_text(system="s", prompt="p") == "plain answer"
    assert "output_config" not in create.calls[0]


def test_openai_json_uses_responses_api_strict_schema():
    llm, create = openai_llm(openai_reply(GOOD))
    out = llm.complete_json(system="sys", prompt="hello", schema=SCHEMA, max_tokens=1000)
    assert out["answer"] == 42
    kw = create.calls[0]
    assert kw["model"] == "gpt-6-sol"
    assert kw["instructions"] == "sys"
    assert kw["input"] == "hello"
    assert kw["store"] is False
    assert kw["max_output_tokens"] >= 1000
    fmt = kw["text"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["strict"] is True
    assert fmt["schema"] == SCHEMA
    assert fmt["name"]


def test_openai_text():
    llm, create = openai_llm(openai_reply("hi there"))
    assert llm.complete_text(system="s", prompt="p") == "hi there"
    assert "text" not in create.calls[0]


def test_gemini_json_uses_response_json_schema():
    llm, generate = gemini_llm(gemini_reply(GOOD))
    out = llm.complete_json(system="sys", prompt="hello", schema=SCHEMA, max_tokens=900)
    assert out["answer"] == 42
    kw = generate.calls[0]
    assert kw["model"] == "gemini-3.1-pro-preview"
    assert kw["contents"] == "hello"
    cfg = kw["config"]
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema == SCHEMA
    assert cfg.system_instruction == "sys"
    assert cfg.max_output_tokens >= 900


def test_gemini_text():
    llm, generate = gemini_llm(gemini_reply("free text"))
    assert llm.complete_text(system="s", prompt="p") == "free text"
    assert generate.calls[0]["config"].response_mime_type is None


# ---------- cost ----------

def test_cost_is_charged_from_usage():
    cost = CostTracker(cap_usd=100)
    llm, _ = anthropic_llm(anthropic_reply(GOOD, tokens=(1_000_000, 100_000)), cost=cost)
    llm.complete_json(system="s", prompt="p", schema=SCHEMA)
    # claude-sonnet-5: $2 in, $10 out per million tokens
    assert cost.spent_usd == pytest.approx(2.0 + 1.0)


def test_gemini_cost_counts_thinking_tokens_as_output():
    cost = CostTracker(cap_usd=100)
    llm, _ = gemini_llm(gemini_reply(GOOD, tokens=(100_000, 50_000, 50_000)), cost=cost)
    llm.complete_json(system="s", prompt="p", schema=SCHEMA)
    # gemini-3.1-pro under 200k input: $2 in, $12 out per million; thinking billed as output
    assert cost.spent_usd == pytest.approx(0.1 * 2.0 + 0.1 * 12.0)


def test_estimate_cost_table_default_and_long_context():
    assert providers.estimate_cost("claude-sonnet-5", 1_000_000, 0) == pytest.approx(2.0)
    assert providers.estimate_cost("gpt-6-luna", 1_000_000, 1_000_000) == pytest.approx(0.6)
    # prefix match: a preview suffix still finds its family
    assert providers.estimate_cost("gemini-3.1-pro-preview", 100_000, 0) == pytest.approx(0.2)
    # long-context tier above 200k input tokens
    assert providers.estimate_cost("gemini-3.1-pro-preview", 300_000, 0) == pytest.approx(1.2)
    unknown = providers.estimate_cost("mystery-model-9", 1_000_000, 1_000_000)
    assert unknown >= max(providers.estimate_cost(m, 1_000_000, 1_000_000) for m in providers.PRICES)


def test_cost_cap_propagates_without_retry():
    cost = CostTracker(cap_usd=0.0001)
    delays: list[float] = []
    llm, create = anthropic_llm(anthropic_reply(GOOD), anthropic_reply(GOOD), cost=cost, delays=delays)
    with pytest.raises(CostCapExceeded):
        llm.complete_json(system="s", prompt="p", schema=SCHEMA)
    assert len(create.calls) == 1
    assert delays == []


# ---------- retries ----------

def test_retryable_error_is_retried_with_backoff():
    delays: list[float] = []
    llm, create = openai_llm(StatusError(429), StatusError(503), openai_reply(GOOD), delays=delays)
    assert llm.complete_json(system="s", prompt="p", schema=SCHEMA)["answer"] == 42
    assert len(create.calls) == 3
    assert len(delays) == 2 and delays[0] < delays[1]


def test_connection_error_without_status_is_retried():
    llm, create = gemini_llm(ConnectionError("reset"), gemini_reply(GOOD))
    assert llm.complete_json(system="s", prompt="p", schema=SCHEMA)["answer"] == 42
    assert len(create.calls) == 2


def test_three_retries_then_llm_error():
    delays: list[float] = []
    llm, create = anthropic_llm(*[StatusError(500)] * 4, delays=delays)
    with pytest.raises(LLMError) as err:
        llm.complete_json(system="s", prompt="p", schema=SCHEMA)
    assert len(create.calls) == 4
    assert len(delays) == 3
    assert "anthropic:claude-sonnet-5" in str(err.value)


def test_client_error_is_not_retried():
    llm, create = anthropic_llm(StatusError(400), anthropic_reply(GOOD))
    with pytest.raises(LLMError):
        llm.complete_json(system="s", prompt="p", schema=SCHEMA)
    assert len(create.calls) == 1


def test_programming_errors_are_not_retried():
    llm, create = gemini_llm(ValueError("bad config"), gemini_reply(GOOD))
    with pytest.raises(LLMError):
        llm.complete_json(system="s", prompt="p", schema=SCHEMA)
    assert len(create.calls) == 1


def test_bad_json_is_retried():
    llm, create = anthropic_llm(anthropic_reply("not json"), anthropic_reply(GOOD))
    assert llm.complete_json(system="s", prompt="p", schema=SCHEMA)["answer"] == 42
    assert len(create.calls) == 2


def test_missing_required_key_is_retried():
    llm, create = openai_llm(openai_reply(json.dumps({"answer": 1})), openai_reply(GOOD))
    assert llm.complete_json(system="s", prompt="p", schema=SCHEMA)["note"] is None
    assert len(create.calls) == 2


def test_truncated_output_is_an_error():
    llm, _ = anthropic_llm(*[anthropic_reply('{"answer": 4', stop="max_tokens")] * 4)
    with pytest.raises(LLMError):
        llm.complete_json(system="s", prompt="p", schema=SCHEMA)
    llm, _ = openai_llm(*[openai_reply("", status="incomplete")] * 4)
    with pytest.raises(LLMError):
        llm.complete_json(system="s", prompt="p", schema=SCHEMA)
    llm, _ = gemini_llm(*[gemini_reply(None, finish="MAX_TOKENS")] * 4)
    with pytest.raises(LLMError):
        llm.complete_json(system="s", prompt="p", schema=SCHEMA)


def test_refusal_is_not_retried():
    llm, create = anthropic_llm(anthropic_reply("", stop="refusal"), anthropic_reply(GOOD))
    with pytest.raises(LLMError):
        llm.complete_json(system="s", prompt="p", schema=SCHEMA)
    assert len(create.calls) == 1


def test_code_fences_are_tolerated():
    llm, _ = gemini_llm(gemini_reply("```json\n" + GOOD + "\n```"))
    assert llm.complete_json(system="s", prompt="p", schema=SCHEMA)["answer"] == 42


# ---------- build ----------

def test_build_rejects_bad_specs():
    with pytest.raises(ValueError):
        providers.build("no-provider-here")
    with pytest.raises(ValueError):
        providers.build("mistral:large")


@pytest.mark.parametrize("spec,env", [("anthropic:claude-sonnet-5", "ANTHROPIC_API_KEY"),
                                      ("openai:gpt-6-sol", "OPENAI_API_KEY"),
                                      ("gemini:gemini-3.1-pro-preview", "GEMINI_API_KEY")])
def test_build_without_key_raises_llm_error(monkeypatch, spec, env):
    monkeypatch.delenv(env, raising=False)
    with pytest.raises(LLMError) as err:
        providers.build(spec)
    assert env in str(err.value)


def test_build_anthropic_with_key(monkeypatch):
    made: list[dict] = []
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: made.append(kw) or SimpleNamespace())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    cost = CostTracker(cap_usd=5)
    llm = providers.build("anthropic:claude-sonnet-5", cost=cost)
    assert llm.spec == "anthropic:claude-sonnet-5" and llm.model == "claude-sonnet-5"
    assert llm.cost is cost
    assert made[0]["api_key"] == "test-key"
    assert made[0]["max_retries"] == 0


def test_build_openai_with_key(monkeypatch):
    made: list[dict] = []
    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: made.append(kw) or SimpleNamespace())
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    llm = providers.build("openai:gpt-6-sol")
    assert isinstance(llm, providers.OpenAILLM)
    assert made[0]["api_key"] == "test-key" and made[0]["max_retries"] == 0


def test_build_gemini_uses_developer_api_not_vertex(monkeypatch):
    made: list[dict] = []
    from google import genai
    monkeypatch.setattr(genai, "Client", lambda **kw: made.append(kw) or SimpleNamespace())
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    llm = providers.build("gemini:gemini-3.1-pro-preview")
    assert isinstance(llm, providers.GeminiLLM)
    assert made[0]["api_key"] == "test-key"
    assert made[0]["enterprise"] is False


def test_build_takes_a_per_use_timeout_and_retry_delays(monkeypatch):
    made: list[dict] = []
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: made.append(kw) or SimpleNamespace())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    default = providers.build("anthropic:claude-sonnet-5")
    assert made[0]["timeout"] == providers.REQUEST_TIMEOUT_SECONDS
    assert (default.delays, default.attempt_seconds, default.max_seconds) == (
        providers.RETRY_DELAYS, providers.REQUEST_TIMEOUT_SECONDS, providers.MAX_CALL_SECONDS)
    short = providers.build("anthropic:claude-sonnet-5", timeout=75, delays=(2.0,))
    assert made[1]["timeout"] == 75
    assert (short.delays, short.attempt_seconds, short.max_seconds) == ((2.0,), 75, providers.call_cap(75))
    with pytest.raises(ValueError):
        providers.build("anthropic:claude-sonnet-5", timeout=0)


def test_gemini_build_passes_the_timeout_in_milliseconds(monkeypatch):
    made: list[dict] = []
    from google import genai
    monkeypatch.setattr(genai, "Client", lambda **kw: made.append(kw) or SimpleNamespace())
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    providers.build("gemini:gemini-3.1-flash-lite", timeout=60, delays=())
    assert made[0]["http_options"].timeout == 60_000


def test_no_delays_means_one_attempt():
    create = Recorder(StatusError(503), anthropic_reply(GOOD))
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    llm = providers.AnthropicLLM("anthropic:claude-sonnet-5", client=client, sleep=no_sleep([]), delays=())
    with pytest.raises(LLMError):
        llm.complete_json(system="s", prompt="p", schema=SCHEMA)
    assert len(create.calls) == 1


def test_factory_builds_one_provider_per_spec_and_options(monkeypatch):
    from nua.llm import LLMFactory
    built = []
    monkeypatch.setattr(providers, "build", lambda spec, **kw: built.append((spec, kw)) or object())
    cost = CostTracker(cap_usd=5)
    llms = LLMFactory(cost)
    plain = llms("anthropic:claude-sonnet-5")
    assert llms("anthropic:claude-sonnet-5") is plain
    short = llms("anthropic:claude-sonnet-5", timeout=75, delays=[])
    assert short is not plain and llms("anthropic:claude-sonnet-5", timeout=75, delays=()) is short
    assert built == [("anthropic:claude-sonnet-5", {"cost": cost}),
                     ("anthropic:claude-sonnet-5", {"cost": cost, "timeout": 75, "delays": ()})]


# ---------- time cap per call ----------

def test_timeouts_stop_retrying_before_the_call_cap():
    """A provider that hangs until the request timeout gets two attempts, not four, so one model call
    stays well inside the task budget."""
    now = [0.0]
    delays: list[float] = []

    class Hang:
        calls = 0

        def __call__(self, **kwargs):
            Hang.calls += 1
            now[0] += providers.REQUEST_TIMEOUT_SECONDS
            raise TimeoutError("read timed out")

    def sleep(seconds):
        delays.append(seconds)
        now[0] += seconds

    client = SimpleNamespace(messages=SimpleNamespace(create=Hang()))
    llm = providers.AnthropicLLM("anthropic:claude-sonnet-5", client=client, sleep=sleep, clock=lambda: now[0])
    with pytest.raises(LLMError) as err:
        llm.complete_json(system="s", prompt="p", schema=SCHEMA)
    assert Hang.calls == 2 and delays == [providers.RETRY_DELAYS[0]]
    assert now[0] <= providers.MAX_CALL_SECONDS
    assert "TimeoutError" in str(err.value)
    from nua.runner import BUDGET_SECONDS
    assert providers.MAX_CALL_SECONDS < BUDGET_SECONDS / 2     # extraction plus verdicts fit the task budget


def test_quick_failures_still_get_every_retry():
    now = [0.0]
    delays: list[float] = []

    def sleep(seconds):
        delays.append(seconds)
        now[0] += seconds

    create = Recorder(*[StatusError(503)] * 3, anthropic_reply(GOOD))
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    llm = providers.AnthropicLLM("anthropic:claude-sonnet-5", client=client, sleep=sleep, clock=lambda: now[0])
    assert llm.complete_json(system="s", prompt="p", schema=SCHEMA)["answer"] == 42
    assert delays == list(providers.RETRY_DELAYS)
