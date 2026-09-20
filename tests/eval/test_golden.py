"""Golden-set skeleton: ≥5 synthetic samples × 3 Etching topics + FakeLLM runner."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_ROOT = REPO_ROOT / "fixtures" / "golden"
TOPICS = ("etching-rf", "etching-materials", "etching-oes")


def _load_runner():
    tools_dir = str(REPO_ROOT / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import run_golden

    return run_golden


def test_golden_set_has_at_least_five_synthetic_samples_per_topic() -> None:
    for topic in TOPICS:
        files = sorted((GOLDEN_ROOT / topic).glob("*.json"))
        assert len(files) >= 5, f"{topic} has {len(files)} samples"
        for path in files:
            sample = json.loads(path.read_text())
            assert sample["synthetic"] is True
            assert sample["blocks"]
            assert sample["labels"]["decision"] in {
                "direct",
                "background",
                "uncertain",
                "unrelated",
            }
            assert "digest" in sample["scripted"]
            assert "verdict" in sample["scripted"]
            assert "extraction" in sample["scripted"]


@pytest.mark.asyncio
async def test_golden_runner_writes_trajectory_and_behavior_placeholder(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    summary = await runner.run_golden(GOLDEN_ROOT, tmp_path)
    assert summary["samples"] >= 15
    assert summary["llm_calls"] >= 15 * 2  # describe + judge; extract extra on direct

    trajectory = json.loads((tmp_path / "trajectory.json").read_text())
    assert isinstance(trajectory, list)
    assert trajectory
    assert {event["event_type"] for event in trajectory} >= {
        "LlmCall",
        "RouteStage",
        "ExtractStage",
    }
    assert all("event_id" in event and "synthetic" in event for event in trajectory)

    behavior = json.loads((tmp_path / "behavior.json").read_text())
    assert behavior["schema_version"] == 2
    assert behavior["content_policy"] == "aggregate-counts-only"
    assert behavior["placeholder"] is True
    assert behavior["model"] == "fake"
    for stage in (
        "source_not_fetched",
        "fetched_not_indexed",
        "recall_miss",
        "route_error",
        "extract_error",
    ):
        assert stage in behavior["leak_stage_counts"]
    assert set(behavior["signals"]) >= {"python_cells", "completion_calls"}

    direct_ids = {
        json.loads(path.read_text())["id"]
        for topic in TOPICS
        for path in (GOLDEN_ROOT / topic).glob("*.json")
        if json.loads(path.read_text())["labels"]["decision"] == "direct"
    }
    extract_events = [
        event for event in trajectory if event["event_type"] == "ExtractStage"
    ]
    assert {event["sample_id"] for event in extract_events} == direct_ids
    assert all((event.get("accepted") or 0) >= 1 for event in extract_events)
    assert behavior["leak_stage_counts"]["extract_error"] == 0
