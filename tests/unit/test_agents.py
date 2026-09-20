"""Unit tests: the seven NOOA adapter agents, routes and gateway (Task 11).

Offline:

- the shipped routes.yaml loads into the unifiedllm registry with the
  L1/L2/L3 tiers pointing at the grok2api endpoint and env-held key;
- tier route resolution is lazy, cached per agent, and honors
  Settings.route_aliases remapping;
- scoped gateway tokens: sign/verify roundtrip, expiry, tampering,
  action grants, job-liveness revocation, unwired seams;
- the InvestigationAgent reaches the gateway through its tool methods
  inside a real CodeAct loop (FakeLLM, inprocess test subclass — the
  sandboxed production config runs on Linux, doc 06 §6/M0);
- factory fail-closed on an empty gateway secret; online-only tools are
  not granted in archive mode.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from nooa import Agent, CodeActStrategy, strategy
from nooa.llm_types import LLMResponse, ToolCall
from nooa.unifiedllm import FakeLLMClient
from nooa.unifiedllm.registry import MODELS

from intel.nooa_adapter import factory
from intel.nooa_adapter.agents import (
    InvestigationAgent,
    RoutingAgent,
    route_client,
)
from intel.nooa_adapter.gateway import (
    GATEWAY_ACTIONS,
    GatewayRejected,
    ScopedTokenPayload,
    sign_scoped_token,
    verify_scoped_token,
)
from intel.settings import Settings

SECRET = "unit-test-gateway-secret"
OWNER = uuid4()
INDUSTRY = uuid4()
JOB = uuid4()


def _active(value=True):
    """Async JobActiveCheck (the production check reads the job store)."""

    async def check(job_id):
        return value

    return check


def _flip_active(state):
    async def check(job_id):
        return state["active"]

    return check


@pytest.fixture
def loaded_routes():
    snapshot = dict(MODELS)
    factory.load_route_registry()
    yield MODELS
    MODELS.clear()
    MODELS.update(snapshot)


class TestRoutesYaml:
    def test_tiers_point_at_grok2api_with_env_key(self, loaded_routes) -> None:
        for tier in ("L1", "L2", "L3"):
            entry = loaded_routes[tier]
            assert entry["api_base"] == Settings().grok2api_base_url
            assert entry["api_key_env"] == "GROK2API_KEY"
            assert entry["model_name"].startswith("openai/grok-")

    def test_route_resolution_is_lazy_and_cached(self, loaded_routes) -> None:
        agent = RoutingAgent(llm=FakeLLMClient())
        client = route_client(agent, "L2")
        assert route_client(agent, "L2") is client

    def test_tier_remap_via_settings(self, loaded_routes, monkeypatch) -> None:
        from intel.nooa_adapter import agents as agents_module

        monkeypatch.setattr(
            agents_module,
            "_settings",
            lambda: Settings(route_aliases={"L2": "L1"}),
        )
        agent = RoutingAgent(llm=FakeLLMClient())
        assert route_client(agent, "L2") is route_client(agent, "L1")

    def test_unknown_tier_rejected(self) -> None:
        with pytest.raises(ValueError):
            route_client(RoutingAgent(llm=FakeLLMClient()), "L9")


class TestScopedTokens:
    def _token(self, *, actions=None, ttl=60, **kwargs) -> str:
        return sign_scoped_token(
            SECRET,
            owner_id=kwargs.get("owner_id", OWNER),
            industry_id=kwargs.get("industry_id", INDUSTRY),
            job_id=kwargs.get("job_id", JOB),
            actions=actions or GATEWAY_ACTIONS,
            ttl_seconds=ttl,
        )

    def test_sign_verify_roundtrip(self) -> None:
        payload = verify_scoped_token(SECRET, self._token())
        assert payload == ScopedTokenPayload(
            owner_id=OWNER,
            industry_id=INDUSTRY,
            job_id=JOB,
            actions=GATEWAY_ACTIONS,
            expires_at=payload.expires_at,
        )
        assert payload.expires_at > datetime.now(UTC)

    def test_expired_token_rejected(self) -> None:
        token = self._token(ttl=-1)
        with pytest.raises(GatewayRejected) as caught:
            verify_scoped_token(SECRET, token)
        assert caught.value.code == "token_expired"

    def test_tampered_token_rejected(self) -> None:
        token = self._token()
        body, signature = token.split(".")
        with pytest.raises(GatewayRejected) as caught:
            verify_scoped_token(SECRET, f"{body[:-2]}xx.{signature}")
        assert caught.value.code == "bad_token"

    def test_wrong_secret_rejected(self) -> None:
        with pytest.raises(GatewayRejected):
            verify_scoped_token("other-secret", self._token())

    def test_empty_secret_fails_closed(self) -> None:
        with pytest.raises(GatewayRejected) as caught:
            sign_scoped_token(
                "",
                owner_id=OWNER,
                industry_id=None,
                job_id=JOB,
                actions={"search_archive"},
                ttl_seconds=60,
            )
        assert caught.value.code == "gateway_unconfigured"

    def test_unknown_action_rejected_at_signing(self) -> None:
        with pytest.raises(GatewayRejected) as caught:
            self._token(actions={"drop_tables"})
        assert caught.value.code == "unknown_actions"


class TestToolGateway:
    def _gateway(self, *, actions=None, active=True, **seams):
        from intel.nooa_adapter.gateway import ToolGateway

        token = sign_scoped_token(
            SECRET,
            owner_id=OWNER,
            industry_id=INDUSTRY,
            job_id=JOB,
            actions=actions or ({"search_archive", "read_evidence"}),
            ttl_seconds=60,
        )
        return ToolGateway(
            token=token,
            secret=SECRET,
            is_job_active=_active(active) if isinstance(active, bool) else active,
            **seams,
        )

    async def test_seam_called_with_verified_payload(self) -> None:
        calls: list = []

        async def seam(payload, *, query, limit):
            calls.append((payload, query, limit))
            return [{"title": "Etch RF tuning", "score": 1.0}]

        gateway = self._gateway(search_archive_fn=seam)
        hits = await gateway.search_archive("RF 蚀刻", limit=3)
        assert hits == [{"title": "Etch RF tuning", "score": 1.0}]
        payload, query, limit = calls[0]
        assert isinstance(payload, ScopedTokenPayload)
        assert payload.owner_id == OWNER and payload.job_id == JOB
        assert query == "RF 蚀刻" and limit == 3

    async def test_action_not_granted(self) -> None:
        gateway = self._gateway()
        with pytest.raises(GatewayRejected) as caught:
            await gateway.search_web("anything")
        assert caught.value.code == "action_not_granted"

    async def test_inactive_job_revokes_tools(self) -> None:
        gateway = self._gateway(active=False)

        async def seam(payload, *, query, limit):  # pragma: no cover
            raise AssertionError("seam must not run")

        gateway._search_archive_fn = seam
        with pytest.raises(GatewayRejected) as caught:
            await gateway.search_archive("q")
        assert caught.value.code == "job_inactive"

    async def test_unwired_seam_is_unavailable(self) -> None:
        gateway = self._gateway()
        with pytest.raises(GatewayRejected) as caught:
            await gateway.read_evidence("ev-1")
        assert caught.value.code == "tool_unavailable"

    async def test_liveness_flips_between_calls(self) -> None:
        state = {"active": True}

        async def seam(payload, *, query, limit):
            return []

        gateway = self._gateway(active=_flip_active(state))
        gateway._search_archive_fn = seam
        assert await gateway.search_archive("one") == []
        state["active"] = False
        with pytest.raises(GatewayRejected):
            await gateway.search_archive("two")


class _InprocessInvestigationAgent(InvestigationAgent):
    """Test stand-in: inprocess backend (the sandbox config runs on Linux
    per doc 06 §6/M0 — macOS cannot host Landlock/seccomp)."""

    @strategy(CodeActStrategy())
    async def investigate(self, question: str) -> list:
        """Investigate the question with the available tools.
        Use the tools, then return the result.
        """
        ...


def _tool_call(name: str, arguments: dict, call_id: str) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


def _resp(content: str = "", tool_calls=None) -> LLMResponse:
    return LLMResponse(
        raw_response=None,
        content=content,
        tool_calls=tool_calls or [],
        finish_reason="tool_calls" if tool_calls else "stop",
    )


class TestInvestigationAgentTools:
    async def test_gateway_reached_inside_codeact_loop(self) -> None:
        archive_calls: list[str] = []

        async def seam(payload, *, query, limit):
            archive_calls.append(query)
            return [{"title": "OES 监控算法", "evidence_id": "ev-9"}]

        from intel.nooa_adapter.gateway import ToolGateway

        token = sign_scoped_token(
            SECRET,
            owner_id=OWNER,
            industry_id=INDUSTRY,
            job_id=JOB,
            actions={"search_archive"},
            ttl_seconds=60,
        )
        gateway = ToolGateway(
            token=token,
            secret=SECRET,
            is_job_active=_active(),
            search_archive_fn=seam,
        )
        hits = [{"title": "OES 监控算法", "evidence_id": "ev-9"}]
        fake = FakeLLMClient(
            scripted_responses=[
                _resp(
                    tool_calls=[
                        _tool_call(
                            "execute_python",
                            {
                                "code": (
                                    "r = await self.search_archive('OES 算法', limit=2)"
                                )
                            },
                            "c1",
                        )
                    ]
                ),
                _resp(tool_calls=[_tool_call("return_result", {"result": hits}, "c2")]),
            ]
        )
        agent = _InprocessInvestigationAgent(llm=fake)
        agent.attach_gateway(gateway)
        result = await agent.investigate("OES 监控现状")
        assert archive_calls == ["OES 算法"]
        assert result == hits

    async def test_ungranted_tool_raises_through_method(self) -> None:
        # search_web not granted → the tool method raises GatewayRejected
        # (fail closed, no degrade). Called directly: inside a CodeAct
        # cell the same raise becomes error feedback the strategy
        # surfaces at its own boundary.
        from intel.nooa_adapter.gateway import ToolGateway

        token = sign_scoped_token(
            SECRET,
            owner_id=OWNER,
            industry_id=INDUSTRY,
            job_id=JOB,
            actions={"search_archive"},
            ttl_seconds=60,
        )
        gateway = ToolGateway(token=token, secret=SECRET, is_job_active=_active())
        agent = _InprocessInvestigationAgent(llm=FakeLLMClient())
        agent.attach_gateway(gateway)
        with pytest.raises(GatewayRejected) as caught:
            await agent.search_web("x")
        assert caught.value.code == "action_not_granted"


class TestFactory:
    def test_gateway_requires_secret(self) -> None:
        settings = Settings(gateway_secret="")
        with pytest.raises(GatewayRejected) as caught:
            factory.make_investigation_gateway(
                settings=settings,
                owner_id=OWNER,
                industry_id=INDUSTRY,
                job_id=JOB,
                is_job_active=_active(),
            )
        assert caught.value.code == "gateway_unconfigured"

    async def test_archive_mode_grants_no_online_actions(self) -> None:
        settings = Settings(gateway_secret=SECRET)

        async def seam(payload, *, query, limit):
            return []

        token, gateway = factory.make_investigation_gateway(
            settings=settings,
            owner_id=OWNER,
            industry_id=INDUSTRY,
            job_id=JOB,
            is_job_active=_active(),
            online=False,
            seams={"search_archive_fn": seam, "search_web_fn": seam},
        )
        payload = verify_scoped_token(SECRET, token)
        assert "fetch_public" not in payload.actions
        assert "search_web" not in payload.actions
        assert "search_archive" in payload.actions
        # Archive mode never granted the online actions, so the gateway
        # rejects them at the action check (the seam is unwired too — the
        # callable was never handed in).
        with pytest.raises(GatewayRejected) as caught:
            await gateway.search_web("q")
        assert caught.value.code == "action_not_granted"

    def test_investigation_agent_gets_gateway(self) -> None:
        settings = Settings(gateway_secret=SECRET)
        _, gateway = factory.make_investigation_gateway(
            settings=settings,
            owner_id=OWNER,
            industry_id=INDUSTRY,
            job_id=JOB,
            is_job_active=_active(),
        )
        agent = factory.make_investigation_agent(gateway=gateway)
        assert agent._gateway is gateway

    def test_all_seven_agent_classes_build(self) -> None:
        for build in (
            factory.make_routing_agent,
            factory.make_extraction_agent,
            factory.make_relation_agent,
            factory.make_evolution_agent,
            factory.make_answer_agent,
            factory.make_query_planner_agent,
        ):
            agent = build()
            assert isinstance(agent, Agent)

    def test_sandbox_config_is_fail_closed(self) -> None:
        from intel.nooa_adapter.agents import investigation_codeact_config

        config = investigation_codeact_config()
        assert config.execution_backend == "sandbox"
        assert config.sandbox.network is False
        assert config.sandbox.require is True


class TestTraceSessionId:
    def test_session_id_matches_exporter_routing(self) -> None:
        from intel.nooa_adapter.tracing import trace_session_id_for

        assert trace_session_id_for(JOB) == f"job-{JOB}"
