"""D16 middleware: the three guardrails installed on every job's agent.

Per design doc 06 §4 (v1.3), each job process constructs its agent
instances with the same three-layer interception:

- ``agent_call``  — MW-02: scoped-token/job-liveness re-check. A cancelled
  job or a lost lease blocks the NEXT agent method call; the LLM is never
  invoked for a dead job.
- ``llm_call``    — MW-01: prompt secret scan (fail closed), per-job LLM
  call budget, and usage collection → the usage sink (model_runs rows in
  production; an in-memory sink in tests).
- ``execute_python`` — MW-03: cell-level output-size defense in depth on
  top of the sandbox's own limits.

中间件抛错传播即 fail-closed：:class:`MiddlewareBlocked` escapes into the
strategy, the job handler classifies it, and the run fails — there is no
degraded continue. The middleware itself is trusted application code and
never widens scope. Note (doc 06 §4): the global
``nooa.runtime.hooks.set_hooks`` slot belongs to tracing — observability
goes through exporters, never that slot.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from nooa.events import ExecutionResult
from nooa.llm_types import LLMResponse
from nooa.runtime.middleware import (
    AgentCallContext,
    AgentCallNext,
    ExecutePythonContext,
    ExecutePythonNext,
    LLMCallContext,
    LLMCallNext,
)

__all__ = [
    "InMemoryUsageSink",
    "LlmBudget",
    "MiddlewareBlocked",
    "ScopeContext",
    "UsageSink",
    "enforce_cell_limits",
    "install_intel_middleware",
    "scan_for_secrets",
]


class MiddlewareBlocked(Exception):
    """A guardrail refused the call — trusted-code fail-closed signal."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(slots=True)
class ScopeContext:
    """What the middleware knows about the running job.

    ``is_active`` is the MW-02 predicate: False once the job is cancelled
    or its fencing lease was lost (the runner owns that check — typically
    a cheap state read against the job store).
    """

    owner_id: UUID
    job_id: UUID
    industry_id: UUID | None = None
    is_active: Callable[[], bool] = lambda: True

    def cancelled_or_lease_lost(self) -> bool:
        return not self.is_active()


# ---------------------------------------------------------------------------
# MW-01a: secret scan
# ---------------------------------------------------------------------------

#: High-precision credential shapes. Deliberately narrow: a false positive
#: kills a legitimate job, so noisy patterns (generic "password=" or hex
#: blobs) stay out; anything ambiguous is left to provider-side key
#: rotation instead of a hard block.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "pem_private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"
        ),
    ),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("openai_api_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
)


def _message_texts(messages: Iterable[Any]) -> Iterable[str]:
    """Yield the text of every message the LLM is about to see."""
    for message in messages:
        if isinstance(message, LLMResponse):
            content = message.content
            if isinstance(content, str):
                yield content
            elif content is not None:
                yield str(content)
            continue
        if isinstance(message, Mapping):
            content = message.get("content")
            if isinstance(content, str):
                yield content
            elif isinstance(content, Sequence) and not isinstance(
                content, (bytes, str)
            ):
                for part in content:
                    if isinstance(part, Mapping):
                        text = part.get("text")
                        if isinstance(text, str):
                            yield text
                    elif isinstance(part, str):
                        yield part


def scan_for_secrets(messages: Iterable[Any]) -> list[str]:
    """Kinds of secret patterns found in the outgoing messages (MW-01).

    Returns matched kind names; empty means clean. High-precision patterns
    only — this gates every LLM call, so a false positive is a dead job.
    """
    hits: list[str] = []
    seen_kinds: set[str] = set()
    for text in _message_texts(messages):
        for kind, pattern in _SECRET_PATTERNS:
            if kind not in seen_kinds and pattern.search(text):
                seen_kinds.add(kind)
                hits.append(kind)
    return hits


# ---------------------------------------------------------------------------
# MW-01b: per-job LLM call budget
# ---------------------------------------------------------------------------


class LlmBudget:
    """Counts LLM calls per job; over the ceiling the call is blocked.

    防失控 (spec 10 §5): one runaway investigation must not spend unbounded
    tokens — the supervisor deadline bounds wall-clock, this bounds calls.
    """

    def __init__(self, max_calls: int) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be >= 1")
        self.max_calls = max_calls
        self._calls: dict[UUID, int] = {}

    def check_llm_call(self, job_id: UUID) -> None:
        used = self._calls.get(job_id, 0) + 1
        if used > self.max_calls:
            raise MiddlewareBlocked(
                "llm_budget_exhausted",
                f"job {job_id} passed its LLM call budget"
                f" ({self.max_calls}); blocking further calls",
            )
        self._calls[job_id] = used

    def calls(self, job_id: UUID) -> int:
        return self._calls.get(job_id, 0)


# ---------------------------------------------------------------------------
# MW-01c: usage collection
# ---------------------------------------------------------------------------


@runtime_checkable
class UsageSink(Protocol):
    """Receives one record per successful LLM call (→ model_runs)."""

    async def record(
        self, scope: ScopeContext, usage: Mapping[str, Any] | None
    ) -> None: ...


class InMemoryUsageSink:
    """Test/dev sink; also the shape the model_runs writer follows."""

    def __init__(self) -> None:
        self.records: list[tuple[UUID, dict[str, Any] | None]] = []

    async def record(
        self, scope: ScopeContext, usage: Mapping[str, Any] | None
    ) -> None:
        self.records.append((scope.job_id, dict(usage) if usage is not None else None))


# ---------------------------------------------------------------------------
# MW-03: cell output limits
# ---------------------------------------------------------------------------


def enforce_cell_limits(result: ExecutionResult, *, max_output_chars: int) -> None:
    """Reject oversized cell output — fail closed, no truncation.

    NOOA's sandbox caps are the primary defense; this is the application
    layer's independent ceiling on what a cell may push back into the
    conversation (doc 06 §4: cell 级超时/输出大小纵深防御).
    """
    for stream_name, stream in (("stdout", result.stdout), ("stderr", result.stderr)):
        if len(stream) > max_output_chars:
            raise MiddlewareBlocked(
                "cell_output_too_large",
                f"cell {stream_name} exceeds {max_output_chars} chars"
                f" (got {len(stream)})",
            )


# ---------------------------------------------------------------------------
# installation
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MiddlewareHandles:
    """Unsubscribe callables for the three installed guards."""

    unsubscribes: list[Callable[[], None]] = field(default_factory=list)

    def uninstall(self) -> None:
        while self.unsubscribes:
            self.unsubscribes.pop()()


def install_intel_middleware(
    agent,
    *,
    scope_ctx: ScopeContext,
    usage_sink: UsageSink,
    budget: LlmBudget,
    max_output_chars: int,
) -> MiddlewareHandles:
    """Install the D16 three-layer guardrails on one agent instance.

    Registration order = execution order (outermost first): the agent_call
    liveness gate wraps everything; the llm_call guard runs inside it;
    execute_python guards sit deepest.
    """
    unsubscribes: list[Callable[[], None]] = []

    async def agent_guard(
        ctx: AgentCallContext, nxt: AgentCallNext
    ) -> AgentCallContext:
        # MW-02: a dead job performs no further agent work at all.
        if scope_ctx.cancelled_or_lease_lost():
            raise MiddlewareBlocked(
                "job_inactive",
                f"job {scope_ctx.job_id} cancelled or lease lost;"
                f" blocking {ctx.method_name!r}",
            )
        return await nxt(ctx)

    async def llm_guard(ctx: LLMCallContext, nxt: LLMCallNext) -> LLMCallContext:
        # MW-01, in order: secrets → budget → the call → usage.
        hits = scan_for_secrets(ctx.messages)
        if hits:
            raise MiddlewareBlocked(
                "secret_in_prompt",
                f"refusing LLM call: {', '.join(hits)} pattern(s) in messages",
            )
        budget.check_llm_call(scope_ctx.job_id)
        ctx = await nxt(ctx)
        usage = ctx.response.usage if ctx.response is not None else None
        await usage_sink.record(
            scope_ctx, usage.model_dump() if usage is not None else None
        )
        return ctx

    async def cell_guard(
        ctx: ExecutePythonContext, nxt: ExecutePythonNext
    ) -> ExecutePythonContext:
        ctx = await nxt(ctx)
        if ctx.result is not None:
            enforce_cell_limits(ctx.result, max_output_chars=max_output_chars)
        return ctx

    unsubscribes.append(agent.event_manager.intercept("agent_call", agent_guard))
    unsubscribes.append(agent.event_manager.intercept("llm_call", llm_guard))
    unsubscribes.append(agent.event_manager.intercept("execute_python", cell_guard))
    return MiddlewareHandles(unsubscribes)
