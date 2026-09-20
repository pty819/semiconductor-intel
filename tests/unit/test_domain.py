"""Unit tests: domain pure functions (time/url), settings, contracts re-exports.

Spec references:
- docs/03-data-model.md §5  时间与历史可见性 (TimeValue semantics)
- docs/04-ingestion-recall.md §3  URL 规范化 (enumeration algorithm)
- docs/10-operations.md §5  配置分类 (Settings fields)
"""

from __future__ import annotations

import inspect
import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from intel.contracts import TimeValue
from intel.domain.time import time_value_bounds
from intel.domain.urlnorm import normalize_url
from intel.settings import Settings

# Fixed +08:00 offset stands in for Asia/Shanghai local time: no tzdata
# dependency, same absolute-midnight arithmetic.
PLUS_8 = timezone(timedelta(hours=8))


# --------------------------------------------------------------------------
# contracts re-exports
# --------------------------------------------------------------------------


def test_contracts_reexport_every_public_model() -> None:
    from intel import contracts
    from intel.contracts import models

    defined = {
        name
        for name, value in vars(models).items()
        if not name.startswith("_")
        and (
            (inspect.isclass(value) and value.__module__ == models.__name__)
            or name == "CorrectionValue"
        )
    }
    assert defined, "models.py unexpectedly exports nothing"
    assert set(contracts.__all__) == defined
    for name in defined:
        assert getattr(contracts, name) is getattr(models, name), name


# --------------------------------------------------------------------------
# URL normalization (spec 04 §3)
# --------------------------------------------------------------------------


class TestNormalizeUrl:
    def test_fragment_stripped(self) -> None:
        assert (
            normalize_url("https://Example.com/docs/guide#section-2")
            == "https://example.com/docs/guide"
        )

    def test_host_lowercased_path_and_query_case_preserved(self) -> None:
        assert (
            normalize_url("https://News.Example.COM/EN/articles?id=3")
            == "https://news.example.com/EN/articles?id=3"
        )

    def test_default_http_port_removed(self) -> None:
        assert normalize_url("http://example.com:80/a") == "http://example.com/a"

    def test_default_https_port_removed(self) -> None:
        assert normalize_url("https://example.com:443/a") == "https://example.com/a"

    def test_empty_port_separator_dropped(self) -> None:
        assert normalize_url("https://example.com:/a") == "https://example.com/a"

    def test_non_default_port_preserved(self) -> None:
        assert (
            normalize_url("https://example.com:8443/a") == "https://example.com:8443/a"
        )

    def test_scheme_mismatched_port_preserved(self) -> None:
        # :80 is only the default for http, not https — must survive.
        assert normalize_url("https://example.com:80/a") == "https://example.com:80/a"

    @pytest.mark.parametrize(
        "param",
        [
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_term",
            "utm_content",
            "fbclid",
            "gclid",
            "msclkid",
        ],
    )
    def test_tracking_params_removed(self, param: str) -> None:
        assert (
            normalize_url(f"https://example.com/p?{param}=x&id=7")
            == "https://example.com/p?id=7"
        )

    @pytest.mark.parametrize(
        "param",
        ["lang", "version", "page", "product", "id", "q"],
    )
    def test_semantic_params_preserved(self, param: str) -> None:
        assert normalize_url(f"https://example.com/p?{param}=v") == (
            f"https://example.com/p?{param}=v"
        )

    def test_spec_04_3_examples(self) -> None:
        # The exact pair used by the spec: ?utm_source=x is deleted,
        # ?lang=zh survives.
        assert (
            normalize_url("https://example.com/p?utm_source=x")
            == "https://example.com/p"
        )
        assert (
            normalize_url("https://example.com/p?lang=zh")
            == "https://example.com/p?lang=zh"
        )

    def test_param_order_stable_no_sorting(self) -> None:
        # Deletion only: the relative order of surviving params is the input
        # order — the output is NOT re-sorted alphabetically.
        assert (
            normalize_url(
                "https://example.com/p?beta=2&alpha=1&utm_source=promo&gamma=3"
            )
            == "https://example.com/p?beta=2&alpha=1&gamma=3"
        )

    def test_query_dropped_when_only_tracking_params(self) -> None:
        assert (
            normalize_url("https://example.com/p?fbclid=abc") == "https://example.com/p"
        )

    def test_tracking_match_is_case_insensitive(self) -> None:
        assert (
            normalize_url("https://example.com/p?UTM_Source=x&lang=zh")
            == "https://example.com/p?lang=zh"
        )

    def test_values_not_reencoded(self) -> None:
        # Percent-encodings and '+' are preserved byte-for-byte.
        assert (
            normalize_url("https://example.com/p?q=a%20b&lang=zh")
            == "https://example.com/p?q=a%20b&lang=zh"
        )
        assert (
            normalize_url("https://example.com/p?q=a+b")
            == "https://example.com/p?q=a+b"
        )

    def test_userinfo_preserved_host_lowercased(self) -> None:
        assert (
            normalize_url("https://user:pw@Example.com/x")
            == "https://user:pw@example.com/x"
        )

    def test_idempotent(self) -> None:
        url = "https://Example.com:443/p?lang=zh&utm_source=x&page=2#frag"
        once = normalize_url(url)
        assert normalize_url(once) == once

    @pytest.mark.parametrize(
        "url",
        ["not a url", "mailto:someone@example.com", "example.com/x"],
    )
    def test_non_http_url_raises(self, url: str) -> None:
        with pytest.raises(ValueError, match="http"):
            normalize_url(url)


# --------------------------------------------------------------------------
# TimeValue bounds (spec 03 §5)
# --------------------------------------------------------------------------


class TestTimeValueBounds:
    def test_unknown_bounds_are_none(self) -> None:
        tv = TimeValue(precision="unknown")
        assert time_value_bounds(tv) == (None, None)

    def test_day_is_local_midnight_interval_in_utc(self) -> None:
        # Day = local midnight to next local midnight, converted to UTC.
        tv = TimeValue(
            precision="day",
            start=datetime(2026, 1, 15, 0, 0, tzinfo=PLUS_8),
            end=datetime(2026, 1, 16, 0, 0, tzinfo=PLUS_8),
        )
        start, end = time_value_bounds(tv)
        assert start == datetime(2026, 1, 14, 16, 0, tzinfo=UTC)
        assert end == datetime(2026, 1, 15, 16, 0, tzinfo=UTC)
        assert start.utcoffset() == end.utcoffset() == timedelta(0)

    def test_day_interval_is_half_open(self) -> None:
        tv = TimeValue(
            precision="day",
            start=datetime(2026, 1, 15, 0, 0, tzinfo=PLUS_8),
            end=datetime(2026, 1, 16, 0, 0, tzinfo=PLUS_8),
        )
        start, end = time_value_bounds(tv)
        # start is inside [start, end); the end instant itself is NOT.
        first_instant, last_excluded = start, end
        assert start <= first_instant < end
        assert not (start <= last_excluded < end)
        # The end instant is exactly the next day's start: adjacent days
        # tile the timeline without overlap.
        next_day = TimeValue(
            precision="day",
            start=datetime(2026, 1, 16, 0, 0, tzinfo=PLUS_8),
            end=datetime(2026, 1, 17, 0, 0, tzinfo=PLUS_8),
        )
        assert time_value_bounds(next_day)[0] == end

    def test_month_precision_does_not_fabricate_a_day(self) -> None:
        # A month-precision value covers [first instant of month,
        # first instant of next month) — no specific day is invented.
        tv = TimeValue(
            precision="month",
            start=datetime(2026, 3, 1, 0, 0, tzinfo=PLUS_8),
            end=datetime(2026, 4, 1, 0, 0, tzinfo=PLUS_8),
        )
        start, end = time_value_bounds(tv)
        assert start == datetime(2026, 2, 28, 16, 0, tzinfo=UTC)
        assert end == datetime(2026, 3, 31, 16, 0, tzinfo=UTC)

    def test_year_bounds(self) -> None:
        tv = TimeValue(
            precision="year",
            start=datetime(2026, 1, 1, 0, 0, tzinfo=PLUS_8),
            end=datetime(2027, 1, 1, 0, 0, tzinfo=PLUS_8),
        )
        assert time_value_bounds(tv) == (
            datetime(2025, 12, 31, 16, 0, tzinfo=UTC),
            datetime(2026, 12, 31, 16, 0, tzinfo=UTC),
        )

    def test_range_is_half_open_and_utc_normalized(self) -> None:
        a = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        b = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
        c = datetime(2026, 1, 3, 0, 0, tzinfo=UTC)
        tv = TimeValue(precision="range", start=a, end=b)
        assert time_value_bounds(tv) == (a, b)
        # Half-open adjacency: [a,b) then [b,c) — end of one is start of next.
        assert time_value_bounds(TimeValue(precision="range", start=b, end=c))[0] == b

    def test_instant_is_a_point(self) -> None:
        t = datetime(2026, 5, 1, 8, 30, tzinfo=PLUS_8)
        assert time_value_bounds(TimeValue(precision="instant", start=t)) == (
            datetime(2026, 5, 1, 0, 30, tzinfo=UTC),
            datetime(2026, 5, 1, 0, 30, tzinfo=UTC),
        )

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            (
                {"precision": "unknown", "start": datetime(2026, 1, 1, tzinfo=UTC)},
                "fabricated bounds",
            ),
            (
                {
                    "precision": "instant",
                    "start": datetime(2026, 1, 1, tzinfo=UTC),
                    "end": datetime(2026, 1, 2, tzinfo=UTC),
                },
                "start only",
            ),
            ({"precision": "day", "start": None}, "requires start"),
            (
                {
                    "precision": "range",
                    "start": datetime(2026, 1, 2, tzinfo=UTC),
                    "end": datetime(2026, 1, 2, tzinfo=UTC),
                },
                "nonempty half-open",
            ),
            (
                {
                    "precision": "range",
                    "start": datetime(2026, 1, 3, tzinfo=UTC),
                    "end": datetime(2026, 1, 2, tzinfo=UTC),
                },
                "nonempty half-open",
            ),
        ],
    )
    def test_invalid_timevalues_raise_in_dto(self, kwargs: dict, match: str) -> None:
        with pytest.raises(ValidationError, match=match):
            TimeValue(**kwargs)

    def test_bounds_guard_against_validator_bypass(self) -> None:
        # model_construct skips validation; time_value_bounds must not
        # silently return a broken interval for such values.
        tv = TimeValue.model_construct(
            precision="range",
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=None,
        )
        with pytest.raises(ValueError, match="end"):
            time_value_bounds(tv)


# --------------------------------------------------------------------------
# Settings (spec 10 §5)
# --------------------------------------------------------------------------


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.upper().startswith(
            ("INTEL_", "GROK2API", "LLM_API_KEY", "LLM_BASE_URL")
        ):
            monkeypatch.delenv(name, raising=False)


class TestSettings:
    def test_defaults(self, clean_env: None) -> None:
        s = Settings(_env_file=None)
        assert s.database_url == "postgresql+asyncpg://intel:intel@localhost:5432/intel"
        assert s.object_store_root == Path("var/objects")
        assert s.session_pepper  # non-empty dev default
        assert s.llm_base_url == "http://192.168.1.82:8080/v1"
        assert s.llm_api_key == ""
        assert s.llm_concurrency == 4
        assert s.fetch_concurrency == 8
        assert s.search_backfill_days == 90
        assert s.bm25_k1 > 0.0
        assert 0.0 <= s.bm25_b <= 1.0
        assert s.diskann_query_search_list_size > 0
        assert s.diskann_query_rescore > 0
        assert s.route_aliases == {"L1": "L1", "L2": "L2", "L3": "L3"}

    def test_env_override_with_intel_prefix(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("INTEL_LLM_CONCURRENCY", "7")
        monkeypatch.setenv("INTEL_SEARCH_BACKFILL_DAYS", "30")
        assert Settings(_env_file=None).llm_concurrency == 7
        assert Settings(_env_file=None).search_backfill_days == 30

    def test_route_aliases_from_env_json(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "INTEL_ROUTE_ALIASES", '{"L1": "cheap-fast", "L2": "mid", "L3": "deep"}'
        )
        s = Settings(_env_file=None)
        assert s.route_aliases == {"L1": "cheap-fast", "L2": "mid", "L3": "deep"}

    def test_llm_prefixed_alias(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("INTEL_LLM_BASE_URL", "http://elsewhere:9/v1")
        monkeypatch.setenv("INTEL_LLM_API_KEY", "sk-x")
        s = Settings(_env_file=None)
        assert s.llm_base_url == "http://elsewhere:9/v1"
        assert s.llm_api_key == "sk-x"

    def test_grok2api_prefixed_alias_still_accepted(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Legacy env spellings keep older .env files working.
        monkeypatch.setenv("INTEL_GROK2API_BASE_URL", "http://elsewhere:9/v1")
        monkeypatch.setenv("INTEL_GROK2API_KEY", "sk-x")
        s = Settings(_env_file=None)
        assert s.llm_base_url == "http://elsewhere:9/v1"
        assert s.llm_api_key == "sk-x"

    def test_grok2api_legacy_unprefixed_alias(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The .env.example written in Task 0 uses unprefixed GROK2API_*.
        monkeypatch.setenv("GROK2API_BASE_URL", "http://legacy:1/v1")
        monkeypatch.setenv("GROK2API_KEY", "sk-legacy")
        s = Settings(_env_file=None)
        assert s.llm_base_url == "http://legacy:1/v1"
        assert s.llm_api_key == "sk-legacy"
