"""Ingestion sources package: adapters, fetcher, SSRF guard (spec 04 §2-4).

The pipeline this package implements:

1. adapters enumerate an entrance into :class:`DiscoveryPage` candidates
   (cursor protocol + overlap window, 04 §3);
2. :class:`~intel.sources.fetcher.HttpFetcher` performs guarded
   conditional fetches (04 §4, SEC-02) with the
   :class:`~intel.sources.browser.BrowserRenderer` fallback;
3. :mod:`intel.workflows.ingest` commits candidates/captures/jobs in
   one transaction per page — nothing in this package persists.
"""

from intel.sources.adapters import (
    DiscoveryAdapter,
    make_adapter,
    validate_adapter_config,
)
from intel.sources.blobstore import (
    FileObjectStore,
    MemoryObjectStore,
    ObjectStore,
)
from intel.sources.browser import (
    BrowserRenderer,
    InterceptionLog,
    deterministic_navigation,
    install_guard_interception,
)
from intel.sources.dto import (
    ADAPTER_KINDS,
    CaptureResult,
    DiscoveredItem,
    DiscoverLimits,
    DiscoveryPage,
    FeedPlan,
    FetchRequest,
)
from intel.sources.fetcher import BrowserUnavailable, HttpFetcher
from intel.sources.pageclient import (
    HttpPageClient,
    PageBodyTooLarge,
    PageClient,
    PageFetchError,
    PageResponse,
)
from intel.sources.politeness import PolitenessGate
from intel.sources.ssrf import (
    AsyncioResolver,
    GuardedNetworkBackend,
    GuardNotInstalled,
    Resolver,
    UrlBlockedError,
    UrlGuard,
    ValidatedTarget,
    assert_guarded_client,
    guarded_async_client,
)

__all__ = [
    "ADAPTER_KINDS",
    "AsyncioResolver",
    "BrowserRenderer",
    "BrowserUnavailable",
    "CaptureResult",
    "DiscoverLimits",
    "DiscoveredItem",
    "DiscoveryAdapter",
    "DiscoveryPage",
    "FeedPlan",
    "FetchRequest",
    "FileObjectStore",
    "GuardNotInstalled",
    "GuardedNetworkBackend",
    "HttpFetcher",
    "HttpPageClient",
    "InterceptionLog",
    "MemoryObjectStore",
    "ObjectStore",
    "PageBodyTooLarge",
    "PageClient",
    "PageFetchError",
    "PageResponse",
    "PolitenessGate",
    "Resolver",
    "UrlBlockedError",
    "UrlGuard",
    "ValidatedTarget",
    "assert_guarded_client",
    "deterministic_navigation",
    "guarded_async_client",
    "install_guard_interception",
    "make_adapter",
    "validate_adapter_config",
]
