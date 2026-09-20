"""Application settings (spec 10 §5 配置分类).

Loaded from the environment with the ``INTEL_`` prefix and an optional
``.env`` file at the working directory. Complex fields (e.g. ``route_aliases``)
are provided as JSON strings. Secrets (DB password, model credentials, session
pepper) must be injected via env at runtime — the defaults here are
development stand-ins only.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the semiconductor-intel service."""

    model_config = SettingsConfigDict(
        env_prefix="INTEL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- infrastructure ----------------------------------------------------
    database_url: str = "postgresql+asyncpg://intel:intel@localhost:5432/intel"
    object_store_root: Path = Path("var/objects")
    # Secret used to derive session cookie signatures. Dev default only;
    # production injects a real pepper via INTEL_SESSION_PEPPER.
    session_pepper: str = "dev-only-session-pepper-change-me"

    # -- model provider ----------------------------------------------------
    # Any OpenAI-v1-compatible chat-completions endpoint (proxy, gateway,
    # or direct). Env names, INTEL_-prefixed first; the legacy
    # GROK2API_* spellings keep older .env files working.
    llm_base_url: str = Field(
        default="http://192.168.1.21:8000/v1",
        validation_alias=AliasChoices(
            "intel_llm_base_url",
            "llm_base_url",
            "intel_grok2api_base_url",
            "grok2api_base_url",
        ),
    )
    llm_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "intel_llm_api_key",
            "llm_api_key",
            "intel_grok2api_key",
            "grok2api_key",
        ),
    )

    # -- runtime knobs (spec 10 §5 防失控配置) ------------------------------
    llm_concurrency: int = 4
    fetch_concurrency: int = 8
    search_backfill_days: int = 90

    # -- parse quality thresholds (spec 04 §4; Task 8) ---------------------
    # usable-text chars / capture bytes outside [min, max] is an anomaly.
    min_text_ratio: float = 0.01
    max_text_ratio: float = 0.95
    # Removed boilerplate share above this flags template-heavy pages.
    boilerplate_max: float = 0.6
    # Below this many usable characters the parse is an abstract at best,
    # and a low text ratio becomes a hard failure (PAR-03).
    min_text_chars: int = 200
    # Link-text share over which a page reads as a recommendation list.
    link_density_max: float = 0.5

    # -- retrieval tuning ----------------------------------------------------
    bm25_k1: float = 1.5
    bm25_b: float = 0.75
    diskann_query_search_list_size: int = 100
    diskann_query_rescore: int = 50

    # -- model routing -------------------------------------------------------
    # L1 量大浅判 / L2 结构化抽取与撰写 / L3 判断与综合 (spec 14 §3.1).
    # Values are unifiedllm registry route aliases.
    route_aliases: dict[str, str] = Field(
        default_factory=lambda: {"L1": "L1", "L2": "L2", "L3": "L3"}
    )

    # -- NOOA middleware knobs (D16: 防失控) ----------------------------------
    # Per-job LLM call ceiling counted in the llm_call middleware; exceeding
    # it blocks the call (MW-01 budget leg) and fails the job.
    llm_max_calls_per_job: int = 400
    # execute_python defense-in-depth cap on captured stdout/stderr size
    # (MW-03). NOOA's own sandbox limits stay primary; this is the app layer.
    cell_output_max_chars: int = 100_000

    # -- investigation gateway (14 §1 scoped tokens) --------------------------
    # HMAC secret for gateway tool tokens. Empty FAILS CLOSED: the ToolGateway
    # refuses to construct (the runner must inject a real secret at startup).
    gateway_secret: str = ""
    gateway_token_ttl_seconds: int = 3600
