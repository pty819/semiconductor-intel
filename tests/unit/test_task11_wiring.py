"""Task 11 entrypoint wiring acceptance (I-5): per-job trace session,
SQL usage sink, investigation summarizer.

One job run through the composition root's _wrap_per_job must produce
≥1 model_runs row (the SQL usage sink) and ≥1 JSONL trace file under the
per-job trace directory — the acceptance the final review asked for,
with fakes (no DB, no live LLM).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

from nooa import Agent, PredictStrategy, strategy
from nooa.unifiedllm import FakeLLMClient
from pydantic import BaseModel

from intel.nooa_adapter.factory import install_investigation_summarizer
from intel.nooa_adapter.tracing import SqlUsageSink, trace_session_id_for
from intel.repositories.base import IndustryScope
from intel.settings import Settings
from intel.workers.composition import _wrap_per_job

OWNER = uuid4()
INDUSTRY = uuid4()


class _Description(BaseModel):
    text: str


class ProbeAgent(Agent):
    @strategy(PredictStrategy())
    async def describe(self, text: str) -> _Description:
        """Describe the text briefly."""
        ...


class _Begin:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc) -> bool:
        return False


class _RecordingConn:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, stmt, parameters=None):
        self.statements.append(getattr(stmt, "text", None) or str(stmt))

    def begin(self) -> _Begin:
        return _Begin(self)


class _RecordingEngine:
    def __init__(self) -> None:
        self.conn = _RecordingConn()

    def connect(self) -> _Begin:
        return _Begin(self.conn)


class TestPerJobWiring:
    async def test_job_run_writes_model_runs_row_and_trace_file(self, tmp_path):
        job_id = uuid4()
        response = json.dumps({"text": "光刻胶供应紧张"})
        # FakeLLMClient needs scripted responses shaped like LLMResponse;
        # reuse the middleware test's helper shape inline.
        from nooa.llm_types import LLMResponse

        fake = FakeLLMClient(
            scripted_responses=[
                LLMResponse(
                    raw_response=None,
                    content=response,
                    tool_calls=[],
                    finish_reason="stop",
                    usage=None,
                )
            ]
        )
        agent_factory = lambda: ProbeAgent(llm=fake)

        engine = _RecordingEngine()
        settings = Settings(_env_file=None)
        handler = _wrap_per_job(
            agent_factory=agent_factory,
            make_handler=lambda agent: _call_agent(agent),
            wiring_factory=lambda agent: agent,
            settings=settings,
            sink=SqlUsageSink(engine),
            trace_dir=tmp_path / "traces",
        )
        ctx = SimpleNamespace(
            job=SimpleNamespace(id=job_id, owner_id=OWNER, industry_id=INDUSTRY),
            lease_lost=False,
            scope=IndustryScope(OWNER, INDUSTRY),
        )
        await handler(ctx)

        # ≥1 model_runs row: the usage sink issued the INSERT.
        inserts = [stmt for stmt in engine.conn.statements if "model_runs" in stmt]
        assert inserts, "no model_runs write issued"

        # ≥1 JSONL trace file for this job's session.
        trace_file = tmp_path / "traces" / f"{trace_session_id_for(job_id)}.jsonl"
        assert trace_file.exists(), f"missing {trace_file}"
        assert trace_file.read_text().strip()


def _call_agent(agent):
    async def inner(ctx: SimpleNamespace) -> None:
        result = await agent.describe("先进制程光刻胶供应情况")
        ctx.result = result

    return inner


class TestSqlUsageSink:
    async def test_write_failure_is_logged_not_raised(self):
        class _BrokenEngine:
            def connect(self):
                raise RuntimeError("db down")

        sink = SqlUsageSink(_BrokenEngine())
        scope = SimpleNamespace(owner_id=OWNER, job_id=uuid4(), industry_id=INDUSTRY)
        await sink.record(scope, {"prompt_tokens": 10})  # no raise


class TestInvestigationSummarizer:
    def test_installs_when_client_exposes_window(self):
        class _WindowLLM:
            context_window = 128_000

        agent = ProbeAgent(llm=_WindowLLM())
        summarizer = install_investigation_summarizer(agent, _WindowLLM())
        assert summarizer is not None

    def test_returns_none_without_real_window_no_fallback_guess(self):
        class _OpaqueLLM:
            pass

        agent = ProbeAgent(llm=_OpaqueLLM())
        assert install_investigation_summarizer(agent, _OpaqueLLM()) is None

    async def test_summarizer_drains_on_close(self):
        class _WindowLLM:
            context_window = 128_000

        agent = ProbeAgent(llm=_WindowLLM())
        summarizer = install_investigation_summarizer(agent, _WindowLLM())
        assert summarizer is not None
        await summarizer.aclose()  # the finally-drain path _wrap_per_job runs
