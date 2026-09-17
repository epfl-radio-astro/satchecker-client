"""Shared setup for the resolver, explicit-file and replay tests.

Two things live here and nothing else. The first is *one* explicit policy
builder: the new resolver requires every selection and acquisition rule to be
stated by the caller, so a test that had to spell out twelve keywords to vary
one of them would say nothing about the one it varies. :func:`policy` states
them once; a test overrides the rule under test and leaves the rest visible.
The second is the endpoint stubs the acquisition tests drive, which record what
was asked of them and in what order.

Nothing here imports a consumer. The values in :func:`policy` are the ones both
consumers happen to configure today, but they are this suite's choices, not
defaults the client installs — the whole point of the interface is that it has
none.

The new API is imported at the top, like everything else; the two accessors
below hand back the modules themselves, for the tests that patch a name on one
or check a signature.
"""

from __future__ import annotations

import threading

import pandas as pd

import satchecker_client.replay as replay
import satchecker_client.resolve as resolve
from satchecker_client.records import KIND_TLE
from satchecker_client.replay import (  # noqa: F401  re-exported for the tests
    REPLAY_IDS_FILE,
    REPLAY_RECORDS_FILE,
    load_replay_orbits,
    save_orbits_for_reuse,
    save_replay_orbits,
)
from satchecker_client.resolve import (  # noqa: F401  re-exported for the tests
    OrbitInputError,
    RejectedOrbit,
    read_extra_orbit_dir,
    resolve_orbits,
)

from .tle_helpers import make_catalogue_df, make_omm_catalogue_df, make_record


# ---------------------------------------------------------------------------
# The modules themselves, for patching and introspection
# ---------------------------------------------------------------------------

def resolve_module():
    """:mod:`satchecker_client.resolve`."""
    return resolve


def replay_module():
    """:mod:`satchecker_client.replay`."""
    return replay


def orbit_input_error():
    """The :class:`OrbitInputError` class, for ``pytest.raises``."""
    return OrbitInputError


def replay_file_names() -> tuple[str, str]:
    """``(REPLAY_IDS_FILE, REPLAY_RECORDS_FILE)`` — the two files a pair is."""
    return REPLAY_IDS_FILE, REPLAY_RECORDS_FILE


# ---------------------------------------------------------------------------
# The vocabulary the result types are written in
# ---------------------------------------------------------------------------

#: ``source_order`` groups. ``extra`` is the caller's own records; ``remote``
#: is the managed cache and the service together, because refresh only means
#: anything with both in one group.
GROUP_EXTRA = "extra"
GROUP_REMOTE = "remote"

#: ``ResolvedOrbit.source`` / ``RejectedOrbit.source`` codes. Stable codes, not
#: display labels: each application already has its own wording for these.
SOURCE_EXTRA = "extra"
SOURCE_CACHE = "cache"
SOURCE_SERVICE = "service"

#: ``replacement`` policies.
STRICTLY_FRESHER = "strictly_fresher"
PREFER_SERVICE = "prefer_service"

#: ``EndpointAttempt.status`` values.
ATTEMPT_EMPTY = "empty"
ATTEMPT_USABLE = "usable"
ATTEMPT_OVER_AGE = "over_age"
ATTEMPT_ERROR = "error"
ATTEMPT_NOT_SENT = "not_sent"

#: ``RejectedOrbit.reason_code`` values.
REASON_OVER_AGE = "over_age"
REASON_INVALID = "invalid"

#: ``OrbitResolution.unavailable`` classifications.
UNAVAILABLE_ABSENT = "absent"
UNAVAILABLE_OFFLINE = "offline"
UNAVAILABLE_NOT_ATTEMPTED = "not_attempted"
UNAVAILABLE_INVALID_LOCAL = "invalid_local"

#: ``ResolutionEvent.code`` values, one per fact the plan requires the resolver
#: to report. The result keeps them whether or not a callback is installed.
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

#: The comparison tolerance the age ceilings are applied with: one TLE epoch
#: quantum (1e-8 d) plus the datetime<->JD round trip's slack. Stated here so a
#: test can sit just inside and just outside it; it is a numerical constant, not
#: a freshness policy.
AGE_TOL_DAYS = 3e-8


# ---------------------------------------------------------------------------
# One explicit policy
# ---------------------------------------------------------------------------

#: Every keyword the resolver requires. The whole interface, in one place: a
#: caller states all of it or the call is a ``TypeError``.
REQUIRED_POLICY_KEYWORDS = (
    "source_order",
    "remote_max_age_days",
    "cache_reuse_max_age_days",
    "extra_orbit_max_age_days",
    "replacement",
    "offline",
    "allow_missing_checksum",
    "strict_response",
    "endpoints",
    "fallback",
    "max_workers",
    "cache",
)


def policy(**overrides) -> dict:
    """Every required ``resolve_orbits`` keyword, stated, with *overrides* applied.

    The endpoint default is one that fails if it is called at all, so a test
    that does not mean to reach the service says so by saying nothing.
    """
    settings = {
        "source_order": (GROUP_EXTRA, GROUP_REMOTE),
        "remote_max_age_days": 3.0,
        "cache_reuse_max_age_days": 1.0,
        "extra_orbit_max_age_days": None,
        "replacement": STRICTLY_FRESHER,
        "offline": False,
        "allow_missing_checksum": False,
        "strict_response": True,
        "endpoints": (ForbiddenEndpoint("unexpected-endpoint").pair,),
        "fallback": True,
        "max_workers": 5,
        "cache": None,
    }
    assert set(settings) == set(REQUIRED_POLICY_KEYWORDS)
    settings.update(overrides)
    return settings


# ---------------------------------------------------------------------------
# Endpoint stubs
# ---------------------------------------------------------------------------

#: Distinguishes "``strict_response`` arrived as false" from "it never arrived".
UNSET = object()


class StubEndpoint:
    """One ``(label, callable)`` endpoint pair with scripted per-ID answers.

    *answers* maps a NORAD ID to a DataFrame (its reply), an exception instance
    (its failure), or a callable taking ``(norad_id, epoch_jd)``. An ID with no
    entry gets *default*, which is an empty frame — the service saying it has no
    such record — unless a test says otherwise.
    """

    def __init__(
        self, label, answers=None, default=None, require_strict=False, journal=None
    ):
        self.label = label
        self.answers = dict(answers or {})
        self.default = default
        self.require_strict = require_strict
        #: Shared across endpoints when the *order* they were asked in matters.
        self.journal = journal
        self.calls: list[tuple[int, float]] = []
        self.strict_flags: list = []
        self._lock = threading.Lock()

    @property
    def pair(self) -> tuple[str, "StubEndpoint"]:
        return (self.label, self)

    @property
    def requested(self) -> list[int]:
        return [norad_id for norad_id, _ in self.calls]

    def __call__(self, norad_id, epoch_jd, *, strict_response=UNSET):
        if self.require_strict and strict_response is UNSET:
            raise TypeError(
                f"{self.label} was called without strict_response; the resolver "
                "must forward it explicitly to every endpoint"
            )
        with self._lock:
            self.calls.append((int(norad_id), float(epoch_jd)))
            self.strict_flags.append(strict_response)
            if self.journal is not None:
                self.journal.append((self.label, int(norad_id)))
        answer = self.answers.get(int(norad_id), self.default)
        if callable(answer) and not isinstance(answer, pd.DataFrame):
            answer = answer(int(norad_id), float(epoch_jd))
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            return pd.DataFrame()
        return answer.copy()


class ForbiddenEndpoint:
    """An endpoint that fails the test if it is asked anything at all."""

    def __init__(self, label="forbidden-endpoint"):
        self.label = label

    @property
    def pair(self) -> tuple[str, "ForbiddenEndpoint"]:
        return (self.label, self)

    def __call__(self, *args, **kwargs):
        raise AssertionError(f"{self.label} must not be asked in this test")


def forbid(what: str):
    """A stand-in that fails the test if anything calls it."""

    def guard(*args, **kwargs):
        raise AssertionError(f"{what} must not be called in this test")

    return guard


def forbid_acquisition(monkeypatch) -> None:
    """Make every route to the service fail loudly, at each layer it has one."""
    from satchecker_client import client, service

    monkeypatch.setattr(service, "fetch_nearest_batch", forbid("fetch_nearest_batch"))
    monkeypatch.setattr(client, "_http_get", forbid("client._http_get"))
    monkeypatch.setattr(client, "fetch_nearest_tle", forbid("fetch_nearest_tle"))
    monkeypatch.setattr(client, "fetch_nearest_omm", forbid("fetch_nearest_omm"))


def forbid_cache(monkeypatch) -> None:
    """Make any cache — including one the resolver constructed itself — fail."""
    from satchecker_client.cache import TextOrbitCache

    for method in ("get", "store", "path"):
        monkeypatch.setattr(TextOrbitCache, method, forbid(f"TextOrbitCache.{method}"))


def forbid_file_reads(monkeypatch) -> None:
    """Make the explicit-file readers fail, for the tests that read nothing."""
    from satchecker_client import cache

    monkeypatch.setattr(cache, "read_orbit_file", forbid("read_orbit_file"))
    monkeypatch.setattr(
        cache, "read_legacy_tle_records", forbid("read_legacy_tle_records")
    )


# ---------------------------------------------------------------------------
# Record frames
# ---------------------------------------------------------------------------

def frame_of(kind, pairs, **overrides) -> pd.DataFrame:
    """A normalised one-kind record frame from ``[(norad_id, epoch_jd), ...]``."""
    frame = (
        make_catalogue_df(pairs) if kind == KIND_TLE else make_omm_catalogue_df(pairs)
    )
    for column, value in overrides.items():
        frame[column] = value
    return frame


def records_frame(records) -> pd.DataFrame:
    """A frame of already-built record dicts, positionally indexed."""
    return pd.DataFrame(list(records))


def extra_frame(entries) -> pd.DataFrame:
    """Explicit-record input from ``[(kind, norad_id, epoch_jd, overrides), ...]``."""
    rows = []
    for entry in entries:
        kind, norad_id, epoch_jd = entry[:3]
        overrides = entry[3] if len(entry) > 3 else {}
        rows.append(make_record(kind, norad_id, epoch_jd, **overrides))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

class EventLog:
    """Collects the resolver's structured events, as an ``on_event`` callback."""

    def __init__(self):
        self.events: list = []

    def __call__(self, event):
        self.events.append(event)

    @property
    def codes(self) -> list[str]:
        return [event.code for event in self.events]

    def of(self, code) -> list:
        return [event for event in self.events if event.code == code]

    def for_id(self, norad_id, code=None) -> list:
        return [
            event
            for event in self.events
            if int(norad_id) in tuple(event.norad_ids)
            and (code is None or event.code == code)
        ]
