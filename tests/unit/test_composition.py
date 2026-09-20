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
