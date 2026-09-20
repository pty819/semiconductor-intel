"""Unit tests: D16 middleware guardrails against FakeLLM (doc 06 §4 v1.3).

Offline (no network, no DB):

- MW-01 secret leg: a credential pattern in the outgoing messages blocks
  the call BEFORE the client — fake.call_count stays 0;
- MW-01 usage leg: a successful call records the response usage in the
  sink (the model_runs shape);
- MW-01 budget leg: the per-job call ceiling blocks call N+1;
- MW-02: an inactive job (cancelled / lease lost) blocks the traced
  agent method before any LLM call;
- MW-03: oversized cell stdout/stderr is rejected, sized output passes;
- scan_for_secrets: dict / LLMResponse / content-parts shapes, and clean
  semiconductor text stays clean (no false positives).
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from nooa import Agent, PredictStrategy, strategy
from nooa.events import ExecutionResult
from nooa.llm_types import LLMResponse, LLMUsage
from nooa.unifiedllm import FakeLLMClient
from pydantic import BaseModel

from intel.nooa_adapter.middleware import (
    InMemoryUsageSink,
    LlmBudget,
    MiddlewareBlocked,
    ScopeContext,
    enforce_cell_limits,
    install_intel_middleware,
    scan_for_secrets,
)

OWNER = uuid4()
JOB = uuid4()


class Description(BaseModel):
    text: str


class ProbeAgent(Agent):
    @strategy(PredictStrategy())
    async def describe(self, text: str) -> Description:
        """Describe the text briefly."""
        ...


def _resp(text: str = "ok", **usage_kwargs) -> LLMResponse:
    usage = LLMUsage(**usage_kwargs) if usage_kwargs else None
    return LLMResponse(
        raw_response=None,
        content=json.dumps({"text": text}),
        tool_calls=[],
        finish_reason="stop",
        usage=usage,
    )


def _wired(fake, *, active=True, max_calls=10):
    agent = ProbeAgent(llm=fake)
    sink = InMemoryUsageSink()
    scope = ScopeContext(
        owner_id=OWNER,
        job_id=JOB,
        is_active=(lambda: active) if isinstance(active, bool) else active,
    )
    handles = install_intel_middleware(
        agent,
        scope_ctx=scope,
        usage_sink=sink,
        budget=LlmBudget(max_calls),
        max_output_chars=1000,
    )
    return agent, sink, handles


class TestSecretScan:
    def test_dict_messages_content_hits(self) -> None:
        messages = [{"role": "user", "content": "use key sk-abc123def456ghi789jkl"}]
        assert scan_for_secrets(messages) == ["openai_api_key"]

    def test_pem_block_hits(self) -> None:
        messages = [{"role": "user", "content": "-----BEGIN PRIVATE KEY-----"}]
        assert scan_for_secrets(messages) == ["pem_private_key"]

    def test_llm_response_message_shape(self) -> None:
        messages = [_resp(text="token ghp_" + "a" * 40)]
        assert scan_for_secrets(messages) == ["github_token"]

    def test_content_parts_shape(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "AKIA" + "A" * 16}],
            }
        ]
        assert scan_for_secrets(messages) == ["aws_access_key_id"]

    def test_clean_semiconductor_text_stays_clean(self) -> None:
        # False positives kill legitimate jobs — niche domain vocabulary
        # (etch RF tuning, OES) must never trip the patterns.
        messages = [
            {
                "role": "user",
                "content": "RF 频率 13.56MHz，OES 监控刻蚀速率，sk -> 有时写作"
                " skill 缩写；polymer 选择与 C4F8 流量相关。",
            }
        ]
        assert scan_for_secrets(messages) == []

    def test_empty_messages(self) -> None:
        assert scan_for_secrets([]) == []


class TestLlmGuard:
    async def test_secret_blocks_before_client(self) -> None:
        fake = FakeLLMClient(scripted_responses=[_resp()])
        agent, sink, _ = _wired(fake)
        with pytest.raises(MiddlewareBlocked) as caught:
            await agent.describe("my key: sk-proj-abcdefghijklmnopqrst")
        assert caught.value.code == "secret_in_prompt"
        # MW-01: blocked BEFORE the round-trip — zero provider calls.
        assert fake.call_count == 0
        assert sink.records == []

    async def test_usage_recorded_after_success(self) -> None:
        fake = FakeLLMClient(
            scripted_responses=[_resp(input_tokens=10, output_tokens=5)]
        )
        agent, sink, _ = _wired(fake)
        result = await agent.describe("刻蚀腔体压力曲线")
        assert result.text == "ok"
        assert fake.call_count == 1
        assert len(sink.records) == 1
        recorded_job, recorded_usage = sink.records[0]
        assert recorded_job == JOB
        assert recorded_usage is not None
        assert recorded_usage["input_tokens"] == 10
        assert recorded_usage["output_tokens"] == 5

    async def test_budget_exhaustion_blocks_next_call(self) -> None:
        fake = FakeLLMClient(scripted_responses=[_resp(), _resp()])
        agent, _sink, _ = _wired(fake, max_calls=1)
        assert (await agent.describe("first")).text == "ok"
        with pytest.raises(MiddlewareBlocked) as caught:
            await agent.describe("second")
        assert caught.value.code == "llm_budget_exhausted"
        assert fake.call_count == 1  # the blocked call never reached the client

    async def test_missing_usage_records_none(self) -> None:
        # usage 缺失为 null，不是 0 (spec 03 §7 / 15 §1).
        fake = FakeLLMClient(scripted_responses=[_resp()])
        agent, sink, _ = _wired(fake)
        await agent.describe("text")
        assert sink.records == [(JOB, None)]


class TestAgentGuard:
    async def test_inactive_job_blocks_method(self) -> None:
        fake = FakeLLMClient(scripted_responses=[_resp()])
        agent, _sink, _ = _wired(fake, active=False)
        with pytest.raises(MiddlewareBlocked) as caught:
            await agent.describe("anything")
        assert caught.value.code == "job_inactive"
        assert fake.call_count == 0

    async def test_liveness_flips_between_calls(self) -> None:
        # The runner flips is_active when the lease is lost mid-job; the
        # NEXT agent method observes it (step-boundary cancellation shape).
        state = {"active": True}
        fake = FakeLLMClient(scripted_responses=[_resp()])
        agent, _, _ = _wired(fake, active=(lambda: state["active"]))
        assert (await agent.describe("ok")).text == "ok"
        state["active"] = False
        with pytest.raises(MiddlewareBlocked):
            await agent.describe("ok")
        assert fake.call_count == 1


class TestCellLimits:
    def test_oversized_stdout_rejected(self) -> None:
        result = ExecutionResult(stdout="x" * 1001)
        with pytest.raises(MiddlewareBlocked) as caught:
            enforce_cell_limits(result, max_output_chars=1000)
        assert caught.value.code == "cell_output_too_large"

    def test_oversized_stderr_rejected(self) -> None:
        result = ExecutionResult(stderr="y" * 2000, stdout="fine")
        with pytest.raises(MiddlewareBlocked):
            enforce_cell_limits(result, max_output_chars=1000)

    def test_sized_output_passes(self) -> None:
        result = ExecutionResult(stdout="ok", stderr="warn")
        enforce_cell_limits(result, max_output_chars=1000)  # no raise


class TestHandles:
    async def test_uninstall_restores_calls(self) -> None:
        fake = FakeLLMClient(scripted_responses=[_resp()])
        agent, sink, handles = _wired(fake)
        handles.uninstall()
        # After uninstall a secret-bearing prompt flows again (guards are
        # per-agent install-time wiring, not global state).
        result = await agent.describe("sk-proj-tokenslipthrough12345")
        assert result.text == "ok"
        assert sink.records == []
