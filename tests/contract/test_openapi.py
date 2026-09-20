"""Contract tests: OpenAPI path inventory + DTO schema subset (Task 15).

Compares the live FastAPI path set (routers in ``intel.api.routes``) with
design-pack ``contracts/openapi.json``. A non-empty path diff lists the
missing routes. Explicit exemptions are allowed for paths that Tasks 0–14
intentionally deferred (documented below).

Also generates JSON Schema from ``intel.contracts.models`` and compares the
load-bearing subset against design-pack ``contracts/schemas.json``:
CorrectionCommand discriminated union, TimeValue invariants, and
GenerationViewerLink. The allowed error-code set is 08 §6 plus the four
Task-5 extensions that this pass ratifies.
"""

from __future__ import annotations

import inspect
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError

from intel.api.app import API_PREFIX
from intel.api.errors import _HTTP_CODE_FALLBACK
from intel.api.routes import all_routers
from intel.contracts.models import (
    ClaimErrorCorrection,
    CorrectionCommand,
    DuplicateMergeCorrection,
    EventTimeCorrection,
    GenerationViewerLink,
    RelationErrorCorrection,
    TimeValue,
    TopicAssociationCorrection,
)
from intel.services import errors as service_errors
from intel.services.errors import ServiceError

REPO_ROOT = Path(__file__).resolve().parents[2]
DESIGN_PACK = Path(
    os.environ.get(
        "INTEL_DESIGN_PACK",
        REPO_ROOT.parent / "semiconductor-intel-design",
    )
)
OPENAPI_PATH = DESIGN_PACK / "contracts" / "openapi.json"
SCHEMAS_PATH = DESIGN_PACK / "contracts" / "schemas.json"

# Design paths not shipped in Tasks 0–14. The path-diff assertion still
# fails and lists any missing route that is not on this list.
EXEMPT_MISSING_PATHS: dict[str, str] = {
    "/api/v1/health/ready": (
        "readiness probe not wired in Tasks 0–14; live-only is the current gate"
        " (no DB ping yet)"
    ),
    "/api/v1/industries/{industry_id}/events": (
        "paged event list; v1 knowledge read surface is timeline + GET-by-id"
    ),
    "/api/v1/industries/{industry_id}/events/{event_id}/corrections": (
        "correction command enqueue; review workflow exists, HTTP command later"
    ),
    "/api/v1/industries/{industry_id}/events/{event_id}/read-state": (
        "per-user read-state POST not wired in Task 13/14"
    ),
    "/api/v1/industries/{industry_id}/evidence/{evidence_id}": (
        "evidence-by-id; Task 13 lists evidence via claim revision instead"
    ),
    "/api/v1/industries/{industry_id}/evidence/{evidence_id}/artifact": (
        "artifact byte stream; object-store download not an HTTP route yet"
    ),
    "/api/v1/industries/{industry_id}/entities/{entity_id}": (
        "PATCH entity; Task 13 shipped list/create only"
    ),
    "/api/v1/industries/{industry_id}/topics/{topic_id}/evolution/rebuild": (
        "evolution rebuild job; GET evolution shipped, rebuild enqueue later"
    ),
}

# Live paths that are not in the design pack. Same rule: extras off this
# list fail the gate.
EXEMPT_EXTRA_PATHS: dict[str, str] = {
    "/health/live": (
        "unprefixed liveness for process managers; design also has /api/v1/health/live"
    ),
    "/api/v1/industries/{industry_id}/claims/{claim_revision_id}/evidence": (
        "Task 13 extra: evidence listed by claim revision"
    ),
    "/api/v1/industries/{industry_id}/topics/{topic_id}/evolution/{evolution_id}": (
        "Task 13 extra: GET one evolution by id under the topic"
    ),
}

# docs/08-api.md §6 stable codes.
SPEC_08_ERROR_CODES = {
    "unauthenticated",
    "not_found",
    "csrf_failed",
    "version_conflict",
    "idempotency_conflict",
    "invalid_state_transition",
    "scope_violation",
    "source_access_blocked",
    "parser_failed",
    "model_unavailable",
    "evidence_invalid",
    "insufficient_evidence",
    "sandbox_unavailable",
    "event_cursor_expired",
    "cancelled",
    "validation_error",
}

# Task 5 extensions this contract pass ratifies. Do not drop them from the
# API mapping.
EXTRA_ERROR_CODES = {
    "already_exists",
    "rate_limited",
    "parser_unavailable",
    "internal_error",
}

ALLOWED_ERROR_CODES = SPEC_08_ERROR_CODES | EXTRA_ERROR_CODES

CORRECTION_KINDS = {
    "claim_error",
    "event_time",
    "duplicate",
    "topic_association",
    "relation_error",
}

STRUCTURAL_KEYS = {
    "type",
    "additionalProperties",
    "required",
    "enum",
    "const",
    "discriminator",
    "oneOf",
    "anyOf",
    "$ref",
    "minimum",
    "format",
    "properties",
    "items",
}


def design_paths() -> set[str]:
    spec = json.loads(OPENAPI_PATH.read_text())
    return set(spec["paths"])


def live_paths() -> set[str]:
    """Path set registered by live routers + the health probes on the app."""
    paths = {
        f"{API_PREFIX}{route.path}"
        for router in all_routers
        for route in router.routes
        if isinstance(route, APIRoute)
    }
    paths.add("/health/live")
    paths.add(f"{API_PREFIX}/health/live")
    return paths


def live_error_codes() -> set[str]:
    codes = {cls.code for cls in _service_error_classes()}
    codes.update(_HTTP_CODE_FALLBACK.values())
    return codes


def _service_error_classes() -> list[type[ServiceError]]:
    found: list[type[ServiceError]] = []
    for obj in vars(service_errors).values():
        if inspect.isclass(obj) and issubclass(obj, ServiceError):
            found.append(obj)
    return found


def generate_defs(*models: type) -> dict[str, Any]:
    """JSON Schema $defs generated from live Pydantic DTOs."""
    defs: dict[str, Any] = {}
    for model in models:
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        nested = schema.pop("$defs", {})
        defs.update(nested)
        defs[model.__name__] = schema
    return defs


def structural(node: Any) -> Any:
    """Drop titles/descriptions/defaults; keep the contract-bearing shape."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key in STRUCTURAL_KEYS:
            if key not in node:
                continue
            value = node[key]
            if key == "required" and isinstance(value, list):
                out[key] = sorted(value)
            elif key == "properties" and isinstance(value, dict):
                out[key] = {
                    name: structural(child) for name, child in sorted(value.items())
                }
            elif key == "discriminator" and isinstance(value, dict):
                mapping = value.get("mapping") or {}
                out[key] = {
                    "propertyName": value.get("propertyName"),
                    "mapping": {name: mapping[name] for name in sorted(mapping)},
                }
            elif key in {"oneOf", "anyOf"} and isinstance(value, list):
                out[key] = [structural(child) for child in value]
            else:
                out[key] = structural(value)
        return out
    if isinstance(node, list):
        return [structural(child) for child in node]
    return node


def test_path_diff_vs_design_openapi_lists_missing_routes() -> None:
    """Design paths absent from FastAPI fail the gate with the missing list."""
    missing = sorted(design_paths() - live_paths() - set(EXEMPT_MISSING_PATHS))
    extra = sorted(live_paths() - design_paths() - set(EXEMPT_EXTRA_PATHS))
    assert missing == [], (
        "path diff vs design openapi.json is non-empty; missing routes:\n"
        + "\n".join(f"  {path}" for path in missing)
    )
    assert extra == [], (
        "path diff vs design openapi.json is non-empty; extra live paths:\n"
        + "\n".join(f"  {path}" for path in extra)
    )


def test_exemptions_are_documented_and_still_apply() -> None:
    """An exemption that has since been implemented must be removed."""
    live = live_paths()
    design = design_paths()
    stale_missing = sorted(path for path in EXEMPT_MISSING_PATHS if path in live)
    stale_extra = sorted(path for path in EXEMPT_EXTRA_PATHS if path in design)
    unknown_missing = sorted(
        path for path in EXEMPT_MISSING_PATHS if path not in design
    )
    unknown_extra = sorted(path for path in EXEMPT_EXTRA_PATHS if path not in live)
    assert stale_missing == [], (
        f"implemented but still exempted as missing: {stale_missing}"
    )
    assert stale_extra == [], f"design now includes extra-exempted path: {stale_extra}"
    assert unknown_missing == [], f"exemption not in design openapi: {unknown_missing}"
    assert unknown_extra == [], f"exemption not a live path: {unknown_extra}"
    assert EXEMPT_MISSING_PATHS and EXEMPT_EXTRA_PATHS


def test_correction_command_union_matches_design_schema() -> None:
    design = json.loads(SCHEMAS_PATH.read_text())["$defs"]
    live = generate_defs(
        CorrectionCommand,
        ClaimErrorCorrection,
        EventTimeCorrection,
        DuplicateMergeCorrection,
        TopicAssociationCorrection,
        RelationErrorCorrection,
    )
    live_cmd = structural(live["CorrectionCommand"])
    design_cmd = structural(design["CorrectionCommand"])
    assert live_cmd == design_cmd

    correction = live["CorrectionCommand"]["properties"]["correction"]
    discriminator = correction["discriminator"]
    assert discriminator["propertyName"] == "kind"
    assert set(discriminator["mapping"]) == CORRECTION_KINDS
    refs = {item["$ref"] for item in correction["oneOf"]}
    assert refs == {
        "#/$defs/ClaimErrorCorrection",
        "#/$defs/EventTimeCorrection",
        "#/$defs/DuplicateMergeCorrection",
        "#/$defs/TopicAssociationCorrection",
        "#/$defs/RelationErrorCorrection",
    }
    for kind, ref in discriminator["mapping"].items():
        name = ref.rsplit("/", 1)[-1]
        assert live[name]["properties"]["kind"]["const"] == kind
        assert structural(live[name]) == structural(design[name])


def test_time_value_schema_and_invariants_match_design() -> None:
    design = json.loads(SCHEMAS_PATH.read_text())["$defs"]["TimeValue"]
    live = generate_defs(TimeValue)["TimeValue"]
    assert structural(live) == structural(design)
    assert live["properties"]["precision"]["enum"] == [
        "instant",
        "day",
        "month",
        "year",
        "range",
        "unknown",
    ]
    assert live["properties"]["basis"]["enum"] == ["explicit", "inferred", "unknown"]
    assert "precision" in live["required"]

    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 2, tzinfo=UTC)
    TimeValue(precision="unknown")
    TimeValue(precision="instant", start=start)
    TimeValue(precision="day", start=start, end=end)
    with pytest.raises(ValidationError, match="fabricated bounds"):
        TimeValue(precision="unknown", start=start)
    with pytest.raises(ValidationError, match="requires start"):
        TimeValue(precision="day")
    with pytest.raises(ValidationError, match="start only"):
        TimeValue(precision="instant", start=start, end=end)
    with pytest.raises(ValidationError, match="nonempty half-open"):
        TimeValue(precision="range", start=start, end=start)


def test_generation_viewer_link_schema_and_invariants_match_design() -> None:
    design = json.loads(SCHEMAS_PATH.read_text())["$defs"]["GenerationViewerLink"]
    live = generate_defs(GenerationViewerLink)["GenerationViewerLink"]
    assert structural(live) == structural(design)
    assert "available" in live["required"]

    expires = datetime(2026, 1, 1, tzinfo=UTC)
    GenerationViewerLink(available=False, reason="not_authorized")
    GenerationViewerLink(
        available=True, url="https://viewer.example.invalid/run", expires_at=expires
    )
    with pytest.raises(ValidationError, match="authorized URL"):
        GenerationViewerLink(available=True)
    with pytest.raises(ValidationError, match="must not reveal a URL"):
        GenerationViewerLink(available=False, url="https://viewer.example.invalid/run")


def test_allowed_error_codes_include_four_extra_and_cover_live_mapping() -> None:
    live = live_error_codes()
    missing_extra = sorted(EXTRA_ERROR_CODES - live)
    rogue = sorted(live - ALLOWED_ERROR_CODES)
    assert missing_extra == [], (
        f"ratified extra codes missing from API mapping: {missing_extra}"
    )
    assert rogue == [], f"live error codes outside 08 §6 + extras: {rogue}"
    assert EXTRA_ERROR_CODES <= ALLOWED_ERROR_CODES
