"""Client for the IAU CPS SatChecker orbital-record service, split by responsibility.

- :mod:`satchecker_client.client` — HTTP transport and response normalisation
  for both nearest-record endpoints and the catalogue name search; standard
  library plus pandas, nothing else.
- :mod:`satchecker_client.tle_parse` — TLE line parsing, and the element range
  checks both record kinds share.
- :mod:`satchecker_client.records` — what a record is, when it is valid, and
  what it means; the only place either format is named.
- :mod:`satchecker_client.cache` — validated per-NORAD record storage.
- :mod:`satchecker_client.catalogue` — which satellites a name search leaves in
  play at a given epoch.
- :mod:`satchecker_client.service` — endpoint selection, bounded concurrent
  acquisition, response validation, and resilient cache writes.

What this package deliberately does not decide: which record a given observation
should use, how old a record may be before it is refused, or how a local archive
ranks against the service. Those are application policy, and they live in the
caller. TABASCAL, the original consumer, keeps them in its ``tabascal.orbit``.

The names most callers need are re-exported here.
"""

from ._version import __version__
from .client import (
    BASE_URL,
    HANDOVER_JD,
    OMM_COLUMNS,
    SEARCH_COLUMNS,
    TLE_COLUMNS,
    SatCheckerError,
    SatCheckerRateLimitError,
    SatCheckerResponseError,
    SatCheckerTransportError,
    fetch_nearest_omm,
    fetch_nearest_tle,
    search_satellites,
    set_client_identifier,
    user_agent,
)
from .catalogue import CANDIDATE_COLUMNS, in_orbit_candidates
from .cache import (
    CacheValidationError,
    SearchSnapshot,
    TextOrbitCache,
    read_legacy_tle_records,
    read_orbit_file,
)
from .records import (
    CHECKSUM_STATUS_FIELD,
    CHECKSUM_UNVERIFIED_MISSING,
    CHECKSUM_VERIFIED,
    KIND_OMM,
    KIND_TLE,
    KIND_FIELD,
    RecordKindError,
    record_elements,
    record_epoch_jd,
    record_kind,
    validate_record,
    validated_record,
)
from .service import (
    MAX_WORKERS,
    NearestBatchResult,
    fetch_nearest_batch,
    nearest_endpoints_for,
    store_or_warn,
)

__all__ = [
    "__version__",
    "BASE_URL",
    "HANDOVER_JD",
    "OMM_COLUMNS",
    "SEARCH_COLUMNS",
    "TLE_COLUMNS",
    "fetch_nearest_omm",
    "nearest_endpoints_for",
    "SatCheckerError",
    "SatCheckerRateLimitError",
    "SatCheckerResponseError",
    "SatCheckerTransportError",
    "fetch_nearest_tle",
    "search_satellites",
    "set_client_identifier",
    "user_agent",
    "CANDIDATE_COLUMNS",
    "in_orbit_candidates",
    "CacheValidationError",
    "SearchSnapshot",
    "TextOrbitCache",
    "read_legacy_tle_records",
    "read_orbit_file",
    "CHECKSUM_STATUS_FIELD",
    "CHECKSUM_UNVERIFIED_MISSING",
    "CHECKSUM_VERIFIED",
    "KIND_OMM",
    "KIND_TLE",
    "KIND_FIELD",
    "RecordKindError",
    "record_elements",
    "record_epoch_jd",
    "record_kind",
    "validate_record",
    "validated_record",
    "MAX_WORKERS",
    "NearestBatchResult",
    "fetch_nearest_batch",
    "store_or_warn",
]
