"""JobRunner composition root registers the Task 14/17 kind handlers."""

from __future__ import annotations

from pathlib import Path

from intel.settings import Settings
from intel.workers.composition import (
    REQUIRED_KINDS,
    ROLE_KINDS,
    assert_required_kinds,
    build_runtime,
    registered_kinds,
)


def _runtime(tmp_path: Path, role: str = "all"):
    settings = Settings(
        _env_file=None,
        object_store_root=tmp_path / "objects",
        gateway_secret="composition-test-secret",
    )
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(settings.database_url)
    return build_runtime(settings, role=role, engine=engine)


def test_all_role_registers_required_kinds(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "all")
    assert_required_kinds(runtime.runner, role="all")
    have = set(registered_kinds(runtime.runner))
    assert have.issuperset(REQUIRED_KINDS)
    assert "watch_check" in have
    assert "index" in have


def test_fetch_role_only_claims_fetch(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "fetch")
    assert_required_kinds(runtime.runner, role="fetch")
    assert set(runtime.kinds) == {"fetch"}


def test_research_role_kinds(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "research")
    assert_required_kinds(runtime.runner, role="research")
    assert set(runtime.kinds) == set(ROLE_KINDS["research"])


def test_pipeline_role_excludes_fetch_and_research(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "pipeline")
    assert_required_kinds(runtime.runner, role="pipeline")
    have = set(runtime.kinds)
    assert "fetch" not in have
    assert "archive_answer" not in have
    assert "investigate" not in have
    assert "report_build" not in have
    assert {"discover", "parse", "index", "route", "extract", "event_build"} <= have


def test_cli_worker_help_is_offline() -> None:
    import pytest

    from intel.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["worker", "--help"])
    assert exc.value.code == 0


def test_nooa_is_a_git_pin_with_no_local_paths() -> None:
    """NOOA comes from GitHub at a pinned rev; no host-local paths may leak
    into pyproject/uv.lock/compose/Dockerfile (they break other machines)."""
    repo = Path(__file__).resolve().parents[2]
    pyproject = (repo / "pyproject.toml").read_text()
    assert 'nooa = { git = "https://github.com/NVIDIA-NeMo/labs-OO-Agents.git"' in pyproject
    assert 'rev = "d4d46f78ae0eeaed7d18a466196601e8d16bc101"' in pyproject
    dockerfile = (repo / "deploy" / "Dockerfile.api").read_text()
    compose = (repo / "deploy" / "compose.yaml").read_text()
    lock = (repo / "uv.lock").read_text()
    for name, text in (
        ("pyproject", pyproject),
        ("dockerfile", dockerfile),
        ("compose", compose),
        ("uv.lock", lock),
    ):
        assert "/Users/liyifan" not in text, f"local path leaked into {name}"


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


async def test_worker_openers_set_local_role_intel_app() -> None:
    """extract/event_build/apply_review/report_build (and siblings) SET ROLE
    at open — superuser postgres must not bypass FORCE RLS."""
    from uuid import uuid4

    from intel.repositories.base import IndustryScope
    from intel.workers.stores import (
        sql_answer_opener,
        sql_event_build_opener,
        sql_extract_opener,
        sql_report_opener,
        sql_review_opener,
        sql_route_opener,
    )

    scope = IndustryScope(uuid4(), uuid4())
    factories = (
        sql_extract_opener,
        sql_event_build_opener,
        sql_review_opener,
        sql_report_opener,
        sql_route_opener,
        sql_answer_opener,
    )
    for factory in factories:
        engine = _RecordingEngine()
        async with factory(engine)(scope):
            pass
        joined = "\n".join(engine.conn.statements)
        assert "SET LOCAL ROLE intel_app" in joined, factory.__name__
