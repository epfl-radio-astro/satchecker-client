"""Optional resolver: the caller's selection rules, executed over the sources.

Everything else in this package answers one question at a time — what a record
is, whether it is valid, what the service said. Deciding *which* record an
observation should use means holding several of those answers at once: an
explicit file against the managed cache against two archives, an age ceiling
that says what is acceptable against a reuse threshold that says what is worth a
request, and a failure that must never read as a satellite the catalogue does
not have. Both applications built that machinery, identically and separately.

This module is that machinery, with the policy taken out of it.
:func:`resolve_orbits` requires every selection and acquisition rule as an
explicit keyword and has no defaults for any of them: a default here would be
one application's policy installed in a shared library, silently applied to
every caller that did not think about it. What the caller gets back is
:class:`OrbitResolution` — the accepted records, the near-misses with the limit
that refused them, the failures, and a structured event for each decision.
Whether an incomplete resolution is fatal, and what to say about it, stays with
the caller.

Four outcomes are kept apart because flattening them is how a service failure
becomes a plausible-looking run: a record accepted, a record refused on age, a
request that failed, and an answer that never arrived. Only the first of those
is a record, and only a successful, empty answer from every endpoint the
fallback policy needed is a satellite the archives have nothing for.
"""

from __future__ import annotations

import functools
import math
import numbers
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from . import cache as cache_module
from . import service
from .cache import CacheValidationError, REQUIRED_COLUMNS_BY_KIND
from .client import SatCheckerError, SatCheckerResponseError
from .records import (
    CHECKSUM_STATUS_FIELD,
    CHECKSUM_UNVERIFIED_MISSING,
    KIND_OMM,
    KIND_TLE,
    norad_id_of,
    record_elements,
    record_epoch_jd,
    validated_record,
)
from .service import store_or_warn


__all__ = [
    "EndpointAttempt",
    "OrbitInputError",
    "OrbitResolution",
    "RejectedOrbit",
    "ResolutionEvent",
    "ResolvedOrbit",
    "read_extra_orbit_dir",
    "resolve_orbits",
]


# ---------------------------------------------------------------------------
# The vocabulary results are written in
# ---------------------------------------------------------------------------

#: ``source_order`` groups. ``extra`` is the caller's own records; ``remote`` is
#: the managed cache and the service together, since a refresh only means
#: anything with both in one group.
GROUP_EXTRA = "extra"
GROUP_REMOTE = "remote"
_GROUPS = (GROUP_EXTRA, GROUP_REMOTE)

#: Where an accepted or refused record came from. Stable codes rather than
#: display labels: an application has its own wording for these.
SOURCE_EXTRA = "extra"
SOURCE_CACHE = "cache"
SOURCE_SERVICE = "service"

#: ``replacement`` policies. Strictly-fresher makes a refresh safe by
#: construction — a staler, equally distant or failed answer leaves the run with
#: what it already had.
REPLACEMENT_STRICTLY_FRESHER = "strictly_fresher"
REPLACEMENT_PREFER_SERVICE = "prefer_service"
_REPLACEMENTS = (REPLACEMENT_STRICTLY_FRESHER, REPLACEMENT_PREFER_SERVICE)

#: What one endpoint supplied for one satellite. ``not_sent`` is an endpoint
#: that was never asked — because the fallback policy did not need it, or an
#: outage stopped acquisition — which is not the same as one that answered and
#: had nothing.
ATTEMPT_EMPTY = "empty"
ATTEMPT_USABLE = "usable"
ATTEMPT_OVER_AGE = "over_age"
ATTEMPT_ERROR = "error"
ATTEMPT_NOT_SENT = "not_sent"

#: Why the nearest candidate found for a satellite was not accepted.
REASON_OVER_AGE = "over_age"
REASON_INVALID = "invalid"

#: Why an unresolved satellite has no failure to show for it. An ID refused on
#: age, one with a service failure, and one whose fallback an outage prevented
#: are deliberately absent from this map: their evidence is the rejection, the
#: error and the attempts.
UNAVAILABLE_ABSENT = "absent"
UNAVAILABLE_OFFLINE = "offline"
UNAVAILABLE_NOT_ATTEMPTED = "not_attempted"
UNAVAILABLE_INVALID_LOCAL = "invalid_local"

#: Event codes. One per fact worth reporting; the result keeps every event
#: whether or not a callback is installed.
EVENT_CANDIDATE_REJECTED = "candidate_rejected"
EVENT_SOURCE_SELECTED = "source_selected"
EVENT_CACHE_HIT = "cache_hit"
EVENT_REFRESH_REQUIRED = "refresh_required"
EVENT_REFRESH_SKIPPED = "refresh_skipped"
EVENT_BATCH_STARTED = "batch_started"
EVENT_ENDPOINT_FALLBACK = "endpoint_fallback"
EVENT_OUTAGE = "outage"
EVENT_INCUMBENT_RETAINED = "incumbent_retained"
EVENT_REFRESH_FAILED = "refresh_failed"
EVENT_UNVERIFIED_ACCEPTED = "unverified_accepted"
EVENT_UNVERIFIED_NOT_CACHED = "unverified_not_cached"
EVENT_CACHE_WRITE_FAILED = "cache_write_failed"

#: Comparison slack for the age ceilings: one TLE line-1 epoch quantum (1e-8 d,
#: ~0.9 ms) plus the datetime<->JD round trip's own noise. It is what makes a
#: zero ceiling mean "this epoch, to the precision an epoch is written in"
#: rather than "bit-exact", which no record ever is. A numerical constant, not a
#: freshness policy: a record several milliseconds away is still refused.
_AGE_TOL_DAYS = 3e-8


class OrbitInputError(SatCheckerError, ValueError):
    """An explicitly named orbit file could not be read as the input it claims to be.

    Carries the path, the row where a row-level failure was found (``None`` for
    a file-level one), and the satellite it concerns when that is known, so an
    application can add its own wording without parsing the message. Also a
    ``ValueError``, since that is what the readers this composes raise and what
    the callers catching them already expect.
    """

    def __init__(self, message, *, path=None, row=None, norad_id=None):
        super().__init__(message)
        self.path = path
        self.row = row
        self.norad_id = norad_id


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedOrbit:
    """One accepted record, with everything that decided it was the one."""

    norad_id: int
    record: dict
    source: str
    endpoint: Optional[str]
    provider: Optional[str]
    epoch_jd: float
    offset_days: float  # signed: record epoch minus observation epoch

    @property
    def age_days(self) -> float:
        return abs(self.offset_days)

    @property
    def remote(self) -> bool:
        """True for a record from the service or its managed cache."""
        return self.source != SOURCE_EXTRA


@dataclass(frozen=True)
class RejectedOrbit:
    """The nearest candidate found for a satellite that was not acceptable.

    ``limit_name`` is the *parameter* that refused it — a rejection that does
    not say which ceiling applied cannot be acted on, and the ceilings are
    deliberately independent of one another.
    """

    norad_id: int
    source: str
    endpoint: Optional[str]
    provider: Optional[str]
    epoch_jd: Optional[float]
    offset_days: Optional[float]
    reason_code: str
    ceiling_days: Optional[float] = None
    limit_name: Optional[str] = None

    @property
    def age_days(self) -> Optional[float]:
        return None if self.offset_days is None else abs(self.offset_days)


@dataclass(frozen=True)
class EndpointAttempt:
    """What one endpoint supplied for one satellite, in endpoint order."""

    endpoint: str
    status: str
    error: Optional[Exception] = None
    rejection: Optional[RejectedOrbit] = None


@dataclass(frozen=True)
class ResolutionEvent:
    """One fact about the resolution, for an application to word as it likes."""

    code: str
    norad_ids: tuple = ()
    source: Optional[str] = None
    endpoint: Optional[str] = None
    path: Optional[str] = None
    error: Optional[BaseException] = None
    details: dict = field(default_factory=dict)


@dataclass
class OrbitResolution:
    """The outcome of resolving one observation's satellites, and why."""

    requested: list
    obs_epoch_jd: float
    remote_max_age_days: Optional[float] = None
    cache_reuse_max_age_days: Optional[float] = None
    extra_orbit_max_age_days: Optional[float] = None
    offline: bool = False
    resolved: dict = field(default_factory=dict)
    rejected: dict = field(default_factory=dict)
    #: Why the service could not answer for an ID that stayed unresolved. Kept
    #: apart from ``rejected``: a rejection is a record that was seen and
    #: judged, this is the absence of an answer, and a coverage failure during
    #: an outage otherwise reads as "this satellite does not exist".
    service_errors: dict = field(default_factory=dict)
    #: Why a *refresh* failed for an ID that is resolved anyway. Never fatal —
    #: the run has a record — but the run is not quite the one that was asked
    #: for, and this is the only place that says so.
    refresh_errors: dict = field(default_factory=dict)
    #: Unresolved IDs with no failure behind them, classified. See the
    #: ``UNAVAILABLE_*`` codes for what is deliberately not in here.
    unavailable: dict = field(default_factory=dict)
    attempts: dict = field(default_factory=dict)
    events: list = field(default_factory=list)

    @property
    def missing(self) -> list:
        """Requested IDs with no accepted record, in requested order."""
        return [nid for nid in self.requested if nid not in self.resolved]

    @property
    def complete(self) -> bool:
        return not self.missing

    def norad_ids(self) -> list:
        """Accepted IDs, in requested order — aligned with :meth:`records`."""
        return [nid for nid in self.requested if nid in self.resolved]

    def records(self) -> list:
        """Accepted records, copied, in requested order."""
        return [dict(self.resolved[nid].record) for nid in self.norad_ids()]

    def frame(self) -> pd.DataFrame:
        """Accepted records plus locally derived elements, in requested order.

        The elements are derived here, from the record, rather than read from
        whatever element columns it arrived with: a provider's stale copy and
        the lines it was published with can disagree, and the lines are the
        record.
        """
        records = self.records()
        if not records:
            return pd.DataFrame()
        frame = pd.DataFrame(records)
        frame["NORAD_CAT_ID"] = self.norad_ids()
        derived = pd.DataFrame(
            [record_elements(record) for record in records], index=frame.index
        )
        # Assigned column by column so a legacy file's own element columns are
        # overwritten rather than duplicated beside the derived ones.
        for column in derived.columns:
            frame[column] = derived[column]
        return frame.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Policy validation
# ---------------------------------------------------------------------------

def _is_boolean(value) -> bool:
    """True for a Python or NumPy boolean.

    ``bool`` is an ``Integral`` and ``numpy.bool_`` is finite and equal to 0 or
    1, so both pass an exactness check as satellite 1, and both pass a
    truthiness check as a policy switch. Neither is an identity or a setting.
    """
    return isinstance(value, bool) or getattr(
        getattr(value, "dtype", None), "kind", ""
    ) == "b"


def _missing(value) -> bool:
    """True for the several ways a cell can be absent in these frames."""
    if value is None:
        return True
    try:
        return bool(value != value)  # NaN
    except (TypeError, ValueError):  # an array, which is present whatever it holds
        return False


def _checked_bool(value, name: str) -> bool:
    """A policy switch as a real boolean, never by truthiness.

    ``offline`` decides whether the service is contacted at all and
    ``allow_missing_checksum`` whether unverifiable data is accepted, so a
    near-miss value — ``"false"``, ``0``, ``1.0`` — is an error rather than a
    coercion in whichever direction the accident happens to point.
    """
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be True or False, got {value!r}")
    return value


def _checked_age(value, name: str) -> Optional[float]:
    """An age ceiling in days: ``None`` for no limit, or a finite non-negative."""
    if value is None:
        return None
    if _is_boolean(value) or not isinstance(value, (numbers.Real, str)):
        raise ValueError(f"{name} must be None or a number of days, got {value!r}")
    try:
        days = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{name} must be None or a number of days, got {value!r}"
        ) from error
    if not math.isfinite(days):
        raise ValueError(f"{name} must be finite, got {value!r}")
    if days < 0:
        raise ValueError(f"{name} must not be negative, got {value!r}")
    return days


def _checked_remote_ages(remote_max_age_days, cache_reuse_max_age_days):
    """The two remote ceilings and the one constraint between them.

    A reuse threshold above the hard ceiling lets a cached record suppress the
    request that could have replaced it, and then be refused by the ceiling
    anyway: no record, and no attempt to get one.
    """
    remote = _checked_age(remote_max_age_days, "remote_max_age_days")
    reuse = _checked_age(cache_reuse_max_age_days, "cache_reuse_max_age_days")
    if reuse is not None and remote is not None and reuse > remote:
        raise ValueError(
            f"cache_reuse_max_age_days ({reuse:g}) must not exceed "
            f"remote_max_age_days ({remote:g})"
        )
    return remote, reuse


def _checked_source_order(source_order) -> tuple:
    if isinstance(source_order, (str, bytes)) or source_order is None:
        raise ValueError(
            f"source_order must be a sequence of {list(_GROUPS)}, got "
            f"{source_order!r}"
        )
    try:
        groups = tuple(source_order)
    except TypeError as error:
        raise ValueError(
            f"source_order must be a sequence of {list(_GROUPS)}, got "
            f"{source_order!r}"
        ) from error
    if not groups:
        raise ValueError(f"source_order must name at least one of {list(_GROUPS)}")
    unknown = [group for group in groups if group not in _GROUPS]
    if unknown:
        raise ValueError(
            f"source_order names {unknown!r}; this resolver knows {list(_GROUPS)}"
        )
    if len(set(groups)) != len(groups):
        raise ValueError(f"source_order lists a group twice: {list(groups)}")
    return groups


def _checked_replacement(replacement: str) -> str:
    if replacement not in _REPLACEMENTS:
        raise ValueError(
            f"replacement must be one of {list(_REPLACEMENTS)}, got {replacement!r}"
        )
    return replacement


def _checked_workers(max_workers) -> int:
    if (
        _is_boolean(max_workers)
        or not isinstance(max_workers, numbers.Integral)
        or int(max_workers) < 1
    ):
        raise ValueError(
            f"max_workers must be a positive integer, got {max_workers!r}"
        )
    return int(max_workers)


def _checked_endpoints(endpoints) -> tuple:
    """The endpoints to ask, in the caller's order, each labelled uniquely.

    The label is what every attempt, rejection and event is filed under, so two
    endpoints sharing one makes the whole record of what was asked ambiguous.
    """
    if isinstance(endpoints, (str, bytes)) or endpoints is None:
        raise ValueError(
            f"endpoints must be a sequence of (label, callable) pairs, got "
            f"{endpoints!r}"
        )
    try:
        pairs = list(endpoints)
    except TypeError as error:
        raise ValueError(
            f"endpoints must be a sequence of (label, callable) pairs, got "
            f"{endpoints!r}"
        ) from error
    if not pairs:
        raise ValueError("endpoints must name at least one endpoint to ask")
    checked = []
    labels = set()
    for position, pair in enumerate(pairs):
        try:
            label, fetch_nearest = pair
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"endpoints[{position}] must be a (label, callable) pair, got "
                f"{pair!r}"
            ) from error
        if not callable(fetch_nearest):
            raise ValueError(
                f"endpoints[{position}] ({label!r}) is not callable: {fetch_nearest!r}"
            )
        label = str(label)
        if label in labels:
            raise ValueError(
                f"endpoints lists {label!r} twice; every attempt is filed under "
                "its endpoint's label, so the labels have to be distinct"
            )
        labels.add(label)
        checked.append((label, fetch_nearest))
    return tuple(checked)


def _checked_cache(cache):
    """An explicit cache or ``None``. No path is discovered and none is built.

    Where a cache lives is the application's decision — it names the product,
    the user and the machine — so this never constructs one.
    """
    if cache is None:
        return None
    if not all(callable(getattr(cache, name, None)) for name in ("get", "store", "path")):
        raise ValueError(
            "cache must be a TextOrbitCache (or something with get, store and "
            f"path) or None, got {cache!r}"
        )
    return cache


def _checked_epoch(obs_epoch_jd) -> float:
    """The observation epoch, as a finite Julian Date.

    Never substituted: not today, not a grid's first sample, not the archive
    handover. Every age in the result is measured from this one number.
    """
    if _is_boolean(obs_epoch_jd) or not isinstance(obs_epoch_jd, (numbers.Real, str)):
        raise ValueError(
            f"obs_epoch_jd must be a Julian Date, got {obs_epoch_jd!r}"
        )
    try:
        epoch = float(obs_epoch_jd)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"obs_epoch_jd must be a Julian Date, got {obs_epoch_jd!r}"
        ) from error
    if not math.isfinite(epoch):
        raise ValueError(f"obs_epoch_jd must be a finite Julian Date, got {epoch!r}")
    return epoch


def _checked_norad_id(value, where: str) -> int:
    """One catalogue ID, validated exactly rather than cast.

    :func:`~satchecker_client.records.norad_id_of` does the checking, since it
    is exact where a float conversion is not: ``"25544.0"`` — how an ID written
    by NumPy or read out of a CSV routinely arrives — is 25544, while
    ``"25544.000000000001"`` is refused rather than rounded into the ISS, and an
    ID above 2**53 keeps every digit. What it cannot refuse is a boolean, which
    is an ``Integral`` equal to 0 or 1 and would pass as satellite 1.
    """
    if _is_boolean(value):
        raise ValueError(f"{where} is {value!r}, which is not a catalogue ID")
    return norad_id_of({"NORAD_CAT_ID": value}, where)


def _requested_ids(norad_ids) -> list:
    """Requested IDs, validated exactly, de-duplicated in first-occurrence order.

    ``None`` is an empty request, which is a legitimate thing to ask for.
    """
    if norad_ids is None:
        return []
    if isinstance(norad_ids, (str, bytes)):
        raise ValueError(
            f"norad_ids must be a sequence of NORAD catalogue IDs, got {norad_ids!r}"
        )
    try:
        values = list(norad_ids)
    except TypeError as error:
        raise ValueError(
            f"norad_ids must be a sequence of NORAD catalogue IDs, got {norad_ids!r}"
        ) from error
    out: list = []
    seen: set = set()
    for position, value in enumerate(values):
        norad_id = _checked_norad_id(value, f"norad_ids[{position}]")
        if norad_id not in seen:
            seen.add(norad_id)
            out.append(norad_id)
    return out


# ---------------------------------------------------------------------------
# Explicit directories
# ---------------------------------------------------------------------------

def read_extra_orbit_dir(directory) -> pd.DataFrame:
    """Every orbit table in *directory*, read strictly, concatenated in name order.

    The strict counterpart of
    :func:`~satchecker_client.cache.read_legacy_tle_records`, for a directory a
    user *named*. There, "cannot be read" and "has no record for this
    satellite" must not be the same answer: the second falls through to the
    cache and the service, so the run is built from exactly the records the user
    said not to use, with nothing to say the file they pointed at was never
    read. So every ``*.json`` goes through
    :func:`~satchecker_client.cache.read_orbit_file` and anything it refuses —
    unreadable, malformed, not a table — raises :class:`OrbitInputError` naming
    the file. So does a non-empty table carrying neither kind's columns. An
    explicitly *empty* table is not a failure: that is a completed run stating
    it selected nothing.

    Each row's identity is validated here, while the file it came from is still
    known, and unrequested rows are validated too — a malformed identity that
    survives to a wanted-ID filter simply vanishes from it, which is the failure
    this reader exists to prevent. The returned frame's ``NORAD_CAT_ID`` is an
    exact integer; nothing else is coerced, and no acceptance policy is applied.

    A missing path, or one that is not a directory, is an empty frame. Whether
    that was worth warning about is the application's to judge: it knows whether
    the path came from a default or from the user.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return pd.DataFrame()
    frames = []
    for path in sorted(directory.glob("*.json")):
        try:
            frame = cache_module.read_orbit_file(path)
            if len(frame) and not any(
                all(column in frame.columns for column in required)
                for required in REQUIRED_COLUMNS_BY_KIND.values()
            ):
                raise ValueError(
                    "it carries neither a TLE's "
                    f"{list(REQUIRED_COLUMNS_BY_KIND[KIND_TLE])} nor an OMM's "
                    f"{list(REQUIRED_COLUMNS_BY_KIND[KIND_OMM])}"
                )
        except (CacheValidationError, OSError, ValueError) as error:
            raise OrbitInputError(
                f"orbit file {path} could not be read as an orbit table: {error}",
                path=path,
            ) from error
        if not len(frame):
            continue
        frames.append(_checked_identities(frame, path))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _checked_identities(frame: pd.DataFrame, path: Path) -> pd.DataFrame:
    """*frame* with every row's ``NORAD_CAT_ID`` validated, or an error naming the row."""
    identities = []
    for row_number, row in enumerate(frame.to_dict(orient="records")):
        try:
            identities.append(norad_id_of(row, f"row {row_number}"))
        except ValueError as error:
            raise OrbitInputError(
                f"row {row_number} of orbit file {path} is not filed against a "
                f"satellite: {error}",
                path=path,
                row=row_number,
            ) from error
    frame = frame.copy()
    frame["NORAD_CAT_ID"] = identities
    return frame


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Candidate:
    """One validated record a source offered for one satellite."""

    position: int
    record: dict
    epoch_jd: float
    offset_days: float
    provider: Optional[str]


def resolve_orbits(
    norad_ids,
    obs_epoch_jd,
    *,
    source_order,
    remote_max_age_days,
    cache_reuse_max_age_days,
    extra_orbit_max_age_days,
    replacement,
    offline,
    allow_missing_checksum,
    strict_response,
    endpoints,
    fallback,
    max_workers,
    cache,
    extra_records=None,
    on_event=None,
    log: Callable[[str], None] = print,
) -> OrbitResolution:
    """Resolve each requested NORAD ID at *obs_epoch_jd* under the given rules.

    Returns an :class:`OrbitResolution` and raises nothing for a satellite it
    could not resolve: whether a gap is fatal is the caller's policy, and the
    result carries what is needed to decide — including, for each unresolved ID,
    whether the archives had nothing, whether what they had was too old, whether
    a request failed, or whether nothing was asked.

    Every rule is required, and none has a default:

    ``source_order``
        ``("extra", "remote")``, either alone, in the caller's order. The first
        group that resolves an ID wins, decided per ID. ``extra`` is
        *extra_records*; ``remote`` is the cache and the service together.
    ``remote_max_age_days``
        Hard ceiling for cached and fetched records alike; ``None`` for none.
    ``cache_reuse_max_age_days``
        Below this, an *already acceptable* cached record suppresses the
        request. Never admits a record the hard ceiling refuses.
    ``extra_orbit_max_age_days``
        The ceiling for the caller's own records, independent of the remote one.
        These are the caller's data; the service's policy does not apply to
        them.
    ``replacement``
        ``"strictly_fresher"`` or ``"prefer_service"``, deciding when an answer
        displaces a record already held.
    ``offline``
        Forbids every request without relaxing any ceiling: offline is about
        what can be reached, not about what an acceptable record is.
    ``allow_missing_checksum``
        Applied identically on every route a record can arrive by.
    ``strict_response``
        Forwarded explicitly to each endpoint, so no endpoint is left on the
        library default.
    ``endpoints``
        ``(label, callable)`` pairs in the order to ask them, each callable
        taking ``(norad_id, epoch_jd)``.
        :func:`~satchecker_client.service.nearest_endpoints_for` produces them.
    ``fallback``
        Whether an ID the previous endpoint supplied no acceptable record for
        earns one more request. An outage always stops acquisition, whatever
        this says.
    ``max_workers``
        Passed to :func:`~satchecker_client.service.fetch_nearest_batch`.
    ``cache``
        A :class:`~satchecker_client.cache.TextOrbitCache`, or ``None`` for no
        cache reads and no cache writes. No cache is ever discovered or built
        here.

    *extra_records* is a frame the caller has already read, so each caller keeps
    its own ingestion contract — :func:`read_extra_orbit_dir` for one that
    stops on an unreadable file, ``read_legacy_tle_records`` for one that skips
    it. *on_event* receives each :class:`ResolutionEvent` as it happens, from
    this thread; an exception it raises is the caller's and propagates. *log*
    is the existing low-level diagnostic stream, forwarded to the cache, the
    batch and the cache writes.
    """
    groups = _checked_source_order(source_order)
    remote_max_age, reuse_max_age = _checked_remote_ages(
        remote_max_age_days, cache_reuse_max_age_days
    )
    extra_max_age = _checked_age(extra_orbit_max_age_days, "extra_orbit_max_age_days")
    run = _Resolution(
        groups=groups,
        remote_max_age=remote_max_age,
        reuse_max_age=reuse_max_age,
        extra_max_age=extra_max_age,
        replacement=_checked_replacement(replacement),
        offline=_checked_bool(offline, "offline"),
        allow_missing_checksum=_checked_bool(
            allow_missing_checksum, "allow_missing_checksum"
        ),
        strict_response=_checked_bool(strict_response, "strict_response"),
        endpoints=_checked_endpoints(endpoints),
        fallback=_checked_bool(fallback, "fallback"),
        max_workers=_checked_workers(max_workers),
        cache=_checked_cache(cache),
        extra_records=extra_records,
        on_event=on_event,
        log=log,
        requested=_requested_ids(norad_ids),
        epoch=_checked_epoch(obs_epoch_jd),
    )
    return run.run()


class _Resolution:
    """One run of :func:`resolve_orbits`: its rules, its state, its result."""

    def __init__(
        self,
        *,
        groups,
        remote_max_age,
        reuse_max_age,
        extra_max_age,
        replacement,
        offline,
        allow_missing_checksum,
        strict_response,
        endpoints,
        fallback,
        max_workers,
        cache,
        extra_records,
        on_event,
        log,
        requested,
        epoch,
    ):
        self.groups = groups
        self.remote_max_age = remote_max_age
        self.reuse_max_age = reuse_max_age
        self.extra_max_age = extra_max_age
        self.replacement = replacement
        self.offline = offline
        self.allow_missing_checksum = allow_missing_checksum
        self.strict_response = strict_response
        self.endpoints = endpoints
        self.fallback = fallback
        self.max_workers = max_workers
        self.cache = cache
        self.extra_records = extra_records
        self.on_event = on_event
        self.log = log
        self.epoch = epoch
        self.result = OrbitResolution(
            requested=requested,
            obs_epoch_jd=epoch,
            remote_max_age_days=remote_max_age,
            cache_reuse_max_age_days=reuse_max_age,
            extra_orbit_max_age_days=extra_max_age,
            offline=offline,
        )
        #: IDs that reached the remote group, which is what an attempts entry
        #: means: one row per configured endpoint, asked or not.
        self.attempted: set = set()
        #: ``{norad_id: {endpoint label: EndpointAttempt}}``, filled as answers
        #: land and completed with ``not_sent`` at the end.
        self.answers: dict = {}
        #: IDs whose only local evidence was a record that did not validate.
        self.invalid_local: set = set()

    # -- events ------------------------------------------------------------

    def emit(self, code, norad_ids=(), **fields) -> None:
        event = ResolutionEvent(
            code=code, norad_ids=tuple(int(nid) for nid in norad_ids), **fields
        )
        self.result.events.append(event)
        if self.on_event is not None:
            self.on_event(event)

    # -- the run -----------------------------------------------------------

    def run(self) -> OrbitResolution:
        if not self.result.requested:
            return self.result
        for group in self.groups:
            pending = self.result.missing
            if not pending:
                break
            if group == GROUP_EXTRA:
                self.from_extra(pending)
            else:
                self.from_remote(pending)
        self.finalise()
        return self.result

    # -- the caller's own records ------------------------------------------

    def from_extra(self, pending: list) -> None:
        candidates = self.candidates_of(
            self.extra_records, set(pending), SOURCE_EXTRA, None
        )
        for norad_id in pending:
            offered = candidates.get(norad_id)
            if offered:
                self.consider(
                    norad_id,
                    offered,
                    SOURCE_EXTRA,
                    None,
                    self.extra_max_age,
                    "extra_orbit_max_age_days",
                )

    # -- the cache and the service -----------------------------------------

    def from_remote(self, pending: list) -> None:
        self.attempted.update(pending)
        reusable = self.install_incumbents(pending)
        # A request is suppressed only by a record the ceiling accepted *and*
        # the reuse threshold covers. Without that intersection, a null reuse
        # threshold makes every cached record a hit — including ones the ceiling
        # then refuses — and the satellite is never fetched at all.
        hits = [
            norad_id
            for norad_id in pending
            if norad_id in reusable and norad_id in self.result.resolved
        ]
        to_fetch = [norad_id for norad_id in pending if norad_id not in hits]
        cached = [
            norad_id
            for norad_id in pending
            if norad_id in self.result.resolved
            and self.result.resolved[norad_id].source == SOURCE_CACHE
        ]
        if cached:
            self.emit(EVENT_CACHE_HIT, cached, source=SOURCE_CACHE)
        if hits:
            self.emit(EVENT_REFRESH_SKIPPED, hits, source=SOURCE_CACHE)
        if not to_fetch:
            return
        self.emit(
            EVENT_REFRESH_REQUIRED,
            to_fetch,
            details={"offline": self.offline},
        )
        if not self.offline:
            self.acquire(to_fetch)

    def install_incumbents(self, pending: list) -> set:
        """Put every acceptable cached record in place before any request goes out.

        Two things depend on that ordering. It is what the strictly-fresher rule
        compares an answer against — without an incumbent a staler answer is
        accepted unopposed — and it is what the run falls back on when the
        request never comes back. Returns the IDs whose cached record is also
        within the reuse threshold, which is the separate question of whether to
        ask at all.
        """
        reusable: set = set()
        if self.cache is None:
            return reusable
        for norad_id in pending:
            records = self.cache.get(norad_id, log=self.log)
            if records is None or not len(records):
                continue
            offered = self.candidates_of(
                records, {norad_id}, SOURCE_CACHE, None
            ).get(norad_id)
            if not offered:
                continue
            best = self.nearest(offered)
            if self.within(best.offset_days, self.reuse_max_age):
                reusable.add(norad_id)
            self.consider(
                norad_id,
                offered,
                SOURCE_CACHE,
                None,
                self.remote_max_age,
                "remote_max_age_days",
            )
        return reusable

    def acquire(self, to_fetch: list) -> None:
        """Ask each endpoint in turn for the IDs the previous one did not supply.

        Fallback turns on what an endpoint *supplied*, not on what is already
        held: an ID riding an acceptable but stale cached record is resolved
        from the start, and filtering on that would deny it the second archive
        it was fetched for. An in-ceiling answer suppresses the fallback even
        when the incumbent is closer — asking the other archive anyway would
        make this a global-nearest search across both, which is a different
        acquisition policy and a different request volume.

        An outage is the one thing that stops the sequence outright. The service
        being unable to serve us is not a reason to ask it a different question,
        and the IDs whose fallback it prevented stay unknown rather than absent.
        """
        remaining = list(to_fetch)
        previous = None
        for position, (label, fetch_nearest) in enumerate(self.endpoints):
            if not remaining or (position and not self.fallback):
                return
            if position:
                self.emit(
                    EVENT_ENDPOINT_FALLBACK,
                    remaining,
                    endpoint=label,
                    details={"after": previous},
                )
            self.emit(EVENT_BATCH_STARTED, remaining, endpoint=label)
            batch = service.fetch_nearest_batch(
                remaining,
                self.epoch,
                fetch_nearest=functools.partial(
                    fetch_nearest, strict_response=self.strict_response
                ),
                endpoint=label,
                max_workers=self.max_workers,
                log=self.log,
                allow_missing_checksum=self.allow_missing_checksum,
            )
            served = self.absorb(batch, remaining, label)
            if batch.outage is not None:
                self.emit(
                    EVENT_OUTAGE, remaining, endpoint=label, error=batch.outage
                )
                return
            remaining = [norad_id for norad_id in remaining if norad_id not in served]
            previous = label

    def absorb(self, batch, asked: list, label: str) -> set:
        """Record what one batch supplied, and return the IDs it supplied it for."""
        self.store_history(batch.records)
        candidates = self.candidates_of(
            batch.records, set(asked), SOURCE_SERVICE, label
        )
        # The IDs rows came back for, which is not the same set as the IDs with
        # a usable candidate: a row this resolver refuses leaves neither.
        answered = set(self.identities(batch.records)) & set(asked)
        served: set = set()
        failures = dict(batch.errors)
        for norad_id in asked:
            offered = candidates.get(norad_id)
            error = failures.get(norad_id)
            if offered:
                usable, rejection = self.consider(
                    norad_id,
                    offered,
                    SOURCE_SERVICE,
                    label,
                    self.remote_max_age,
                    "remote_max_age_days",
                )
                if usable:
                    served.add(norad_id)
                self.record_attempt(
                    norad_id,
                    label,
                    ATTEMPT_USABLE if usable else ATTEMPT_OVER_AGE,
                    rejection=rejection,
                )
                continue
            if error is None and norad_id in answered:
                # Rows came back and none of them validated here. That is a
                # response this resolver could not use, not a satellite the
                # archive has no record of, and the two must not read alike.
                error = SatCheckerResponseError(
                    f"{label} returned no record for {norad_id} that this "
                    "resolver's checksum and validity policy accepts"
                )
                failures[norad_id] = error
            self.record_attempt(
                norad_id,
                label,
                ATTEMPT_ERROR if error is not None else ATTEMPT_EMPTY,
                error=error,
            )
        for norad_id, error in failures.items():
            self.record_failure(norad_id, error)
        return served

    def record_failure(self, norad_id: int, error) -> None:
        """File a request failure under whether the run has a record regardless.

        Which of the two it is can still change: an ID a later endpoint or a
        later source group resolves stops being a coverage failure, and
        :meth:`finalise` moves it. It is still a failed request either way, and
        has to stay somewhere the log can find it.
        """
        if norad_id in self.result.resolved:
            self.result.refresh_errors[norad_id] = error
        else:
            self.result.service_errors[norad_id] = error

    def record_attempt(self, norad_id, label, status, error=None, rejection=None) -> None:
        self.answers.setdefault(int(norad_id), {})[label] = EndpointAttempt(
            endpoint=label, status=status, error=error, rejection=rejection
        )

    # -- candidates --------------------------------------------------------

    @staticmethod
    def identities(frame) -> list:
        """The satellites *frame* has rows for, skipping any row not filed against one."""
        found = []
        for row in frame.to_dict(orient="records"):
            try:
                found.append(norad_id_of(row, "record"))
            except ValueError:
                continue
        return found

    def candidates_of(self, frame, wanted: set, source: str, endpoint) -> dict:
        """Validated candidates for *wanted*, by satellite, in input order.

        Rows are read by position: a frame concatenated from several reads
        repeats its index labels, and selecting by label there selects another
        satellite's row. Every candidate is canonicalised through
        :func:`~satchecker_client.records.validated_record` before its epoch is
        read, so one checksum policy applies on every route and a repaired line
        is repaired *on the record* rather than merely tolerated on the way past.
        """
        offered: dict = {}
        if frame is None or not len(frame):
            return offered
        for position, row in enumerate(frame.to_dict(orient="records")):
            try:
                norad_id = norad_id_of(row, "record")
            except ValueError:
                continue  # not filed against a satellite; the reader names those
            if norad_id not in wanted:
                continue
            provider = None if _missing(row.get("DATA_SOURCE")) else row["DATA_SOURCE"]
            try:
                record = validated_record(
                    row, allow_missing_checksum=self.allow_missing_checksum
                )
                epoch_jd = record_epoch_jd(record)
            except (KeyError, ValueError, TypeError) as error:
                self.reject_invalid(norad_id, source, endpoint, provider, error)
                continue
            offered.setdefault(norad_id, []).append(
                _Candidate(
                    position=position,
                    record=record,
                    epoch_jd=epoch_jd,
                    offset_days=epoch_jd - self.epoch,
                    provider=provider,
                )
            )
        return offered

    @staticmethod
    def nearest(candidates: list) -> _Candidate:
        """The candidate nearest the observation; an exact tie keeps the first."""
        return min(candidates, key=lambda c: (abs(c.offset_days), c.position))

    @staticmethod
    def within(offset_days: float, ceiling: Optional[float]) -> bool:
        return ceiling is None or abs(offset_days) <= ceiling + _AGE_TOL_DAYS

    def consider(
        self, norad_id, candidates, source, endpoint, ceiling, limit_name
    ) -> tuple:
        """Judge a source's best candidate for one ID against *ceiling*.

        Returns whether the source supplied an in-ceiling record — which is not
        the same question as whether it changed what is held, since an answer no
        fresher than the incumbent is still an answer — and the rejection if it
        did not.
        """
        best = self.nearest(candidates)
        if not self.within(best.offset_days, ceiling):
            return False, self.reject_over_age(
                norad_id, source, endpoint, best, ceiling, limit_name
            )
        incumbent = self.result.resolved.get(norad_id)
        if incumbent is not None and not self.displaces(incumbent, best):
            self.emit(
                EVENT_INCUMBENT_RETAINED,
                (norad_id,),
                source=incumbent.source,
                endpoint=endpoint,
                details={
                    "offered_offset_days": best.offset_days,
                    "held_offset_days": incumbent.offset_days,
                    "offered_source": source,
                },
            )
            return True, None
        self.result.resolved[norad_id] = ResolvedOrbit(
            norad_id=norad_id,
            record=best.record,
            source=source,
            endpoint=endpoint,
            provider=best.provider,
            epoch_jd=best.epoch_jd,
            offset_days=best.offset_days,
        )
        self.result.rejected.pop(norad_id, None)
        self.emit(
            EVENT_SOURCE_SELECTED,
            (norad_id,),
            source=source,
            endpoint=endpoint,
            details={"offset_days": best.offset_days},
        )
        return True, None

    def displaces(self, incumbent: ResolvedOrbit, candidate: _Candidate) -> bool:
        if self.replacement == REPLACEMENT_PREFER_SERVICE:
            return True
        return abs(candidate.offset_days) < incumbent.age_days

    def reject_over_age(
        self, norad_id, source, endpoint, candidate, ceiling, limit_name
    ) -> RejectedOrbit:
        rejection = RejectedOrbit(
            norad_id=norad_id,
            source=source,
            endpoint=endpoint,
            provider=candidate.provider,
            epoch_jd=candidate.epoch_jd,
            offset_days=candidate.offset_days,
            reason_code=REASON_OVER_AGE,
            ceiling_days=ceiling,
            limit_name=limit_name,
        )
        self.emit(
            EVENT_CANDIDATE_REJECTED,
            (norad_id,),
            source=source,
            endpoint=endpoint,
            details={
                "reason_code": REASON_OVER_AGE,
                "offset_days": candidate.offset_days,
                "ceiling_days": ceiling,
                "limit_name": limit_name,
            },
        )
        self.keep_best_rejection(norad_id, rejection)
        return rejection

    def reject_invalid(self, norad_id, source, endpoint, provider, error) -> None:
        if source != SOURCE_SERVICE:
            self.invalid_local.add(norad_id)
        self.log(f"  {norad_id}: invalid {source} record rejected — {error}")
        self.emit(
            EVENT_CANDIDATE_REJECTED,
            (norad_id,),
            source=source,
            endpoint=endpoint,
            error=error,
            details={"reason_code": REASON_INVALID},
        )
        self.keep_best_rejection(
            norad_id,
            RejectedOrbit(
                norad_id=norad_id,
                source=source,
                endpoint=endpoint,
                provider=provider,
                epoch_jd=None,
                offset_days=None,
                reason_code=REASON_INVALID,
            ),
        )

    def keep_best_rejection(self, norad_id, rejection: RejectedOrbit) -> None:
        """Keep the nearest measurable near-miss, and its own limit, per ID.

        "The best record available was 4.2 d away, against a 3 d ceiling" tells
        the user what to do; "unparseable" does not, so a rejection carrying a
        real offset is never displaced by one without. Worth keeping at all only
        while nothing acceptable is held: a rejected upgrade candidate for a
        satellite that already has a record is not a near-miss.
        """
        if norad_id in self.result.resolved:
            return
        previous = self.result.rejected.get(norad_id)
        if previous is not None:
            if rejection.age_days is None:
                return
            if previous.age_days is not None and previous.age_days <= rejection.age_days:
                return
        self.result.rejected[norad_id] = rejection

    # -- cache writes ------------------------------------------------------

    def store_history(self, records) -> None:
        """Keep everything verifiable that came back, before anything is selected.

        A record refused on age for this observation is the record a run at
        another epoch wants, and it has already been paid for.
        """
        if self.cache is None or records is None or records.empty:
            return
        for norad_id, rows in records.groupby("NORAD_CAT_ID"):
            norad_id = int(norad_id)
            verifiable = self.verifiable(norad_id, rows)
            if verifiable.empty:
                continue
            self.store_or_report(norad_id, verifiable)

    def verifiable(self, norad_id: int, rows: pd.DataFrame) -> pd.DataFrame:
        """*rows* without the records nothing has verified.

        The shared cache is read by every application using this package, at
        whatever version each is on, and an older one refuses a whole file over
        a single line it cannot validate. :meth:`TextOrbitCache.store` already
        leaves out a line with no checksum digit; what it cannot see is a record
        whose lines checksum now but whose source never supplied the digits that
        verify them. Such a record serves this run and is saved with it, and the
        shared cache is left as it was.
        """
        keep = []
        withheld = 0
        for position, row in enumerate(rows.to_dict(orient="records")):
            try:
                status = validated_record(row, allow_missing_checksum=True).get(
                    CHECKSUM_STATUS_FIELD
                )
            except (KeyError, ValueError, TypeError):
                keep.append(position)  # not ours to judge; the cache applies its rule
                continue
            if status == CHECKSUM_UNVERIFIED_MISSING:
                withheld += 1
            else:
                keep.append(position)
        if withheld:
            self.emit(
                EVENT_UNVERIFIED_NOT_CACHED,
                (norad_id,),
                source=SOURCE_SERVICE,
                details={"records": withheld},
            )
        return rows.iloc[keep]

    def store_or_report(self, norad_id: int, rows: pd.DataFrame) -> None:
        failure: list = []

        def write() -> None:
            try:
                self.cache.store(norad_id, rows)
            except OSError as error:
                failure.append(error)
                raise

        path = self.cache.path(norad_id)
        if not store_or_warn(
            write, path, f"orbit cache for NORAD {norad_id}", log=self.log
        ):
            self.emit(
                EVENT_CACHE_WRITE_FAILED,
                (norad_id,),
                source=SOURCE_CACHE,
                path=str(path),
                error=failure[0] if failure else None,
            )

    # -- the outcome -------------------------------------------------------

    def finalise(self) -> None:
        """Complete the attempts, classify what is left, and report the rest."""
        for norad_id in list(self.result.service_errors):
            if norad_id in self.result.resolved:
                self.result.refresh_errors[norad_id] = self.result.service_errors.pop(
                    norad_id
                )
        self.result.attempts = {
            norad_id: [
                self.answers.get(norad_id, {}).get(label)
                or EndpointAttempt(endpoint=label, status=ATTEMPT_NOT_SENT)
                for label, _ in self.endpoints
            ]
            for norad_id in self.result.requested
            if norad_id in self.attempted
        }
        for norad_id in self.result.missing:
            classification = self.classify(norad_id)
            if classification is not None:
                self.result.unavailable[norad_id] = classification
        for norad_id in self.result.requested:
            error = self.result.refresh_errors.get(norad_id)
            entry = self.result.resolved.get(norad_id)
            if error is not None and entry is not None:
                # Named with the source it is continuing from: saying "from the
                # cache" for an ID a second archive answered would describe a
                # record the run never held.
                self.emit(
                    EVENT_REFRESH_FAILED,
                    (norad_id,),
                    source=entry.source,
                    endpoint=entry.endpoint,
                    error=error,
                )
            if entry is not None and entry.record.get(
                CHECKSUM_STATUS_FIELD
            ) == CHECKSUM_UNVERIFIED_MISSING:
                self.emit(
                    EVENT_UNVERIFIED_ACCEPTED,
                    (norad_id,),
                    source=entry.source,
                    endpoint=entry.endpoint,
                    details={"status": CHECKSUM_UNVERIFIED_MISSING},
                )

    def classify(self, norad_id: int) -> Optional[str]:
        """Why one unresolved ID has nothing, when nothing failed for it.

        An ID with a service failure, one refused on age, and one whose required
        fallback an outage prevented are all left out: each already has its own
        evidence, and calling any of them absent would report a satellite the
        archives do have as one they do not.
        """
        if norad_id in self.result.service_errors:
            return None
        attempts = self.result.attempts.get(norad_id, [])
        needed = attempts if self.fallback else attempts[:1]
        completed = [
            attempt for attempt in needed if attempt.status != ATTEMPT_NOT_SENT
        ]
        if norad_id in self.invalid_local and not completed:
            return UNAVAILABLE_INVALID_LOCAL
        if norad_id not in self.attempted:
            return UNAVAILABLE_NOT_ATTEMPTED
        if self.offline:
            return UNAVAILABLE_OFFLINE
        if needed and all(attempt.status == ATTEMPT_EMPTY for attempt in needed):
            return UNAVAILABLE_ABSENT
        return None
