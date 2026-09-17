"""Offline tests for the optional resolver's policy and candidate selection.

The resolver executes rules the caller states; it installs none of its own. So
almost every test here is about a rule being *obeyed exactly*, including the
ones whose obvious-looking shortcut would silently change which orbital record a
simulation propagates: a ceiling applied to the wrong source, a reuse threshold
read as an acceptance threshold, a provider's own epoch field trusted over the
one encoded in a TLE's lines.

The new API is reached through :mod:`tests.resolve_helpers`, which imports it on
use so a missing module fails these tests one at a time rather than aborting
collection.
"""

import inspect
import math

import numpy as np
import pandas as pd
import pytest

from satchecker_client.cache import TextOrbitCache
from satchecker_client.records import (
    KIND_OMM,
    KIND_TLE,
    record_elements,
    record_epoch_jd,
)
from satchecker_client.tle_parse import tle_epoch_jd

from .tle_helpers import (  # noqa: F401  block_network is an autouse fixture
    block_network,
    both_kinds,
    jd,
    make_catalogue_df,
    make_omm,
    make_tle,
    make_tle_record,
)
from .resolve_helpers import (
    AGE_TOL_DAYS,
    ATTEMPT_NOT_SENT,
    ATTEMPT_USABLE,
    EVENT_CACHE_WRITE_FAILED,
    GROUP_EXTRA,
    GROUP_REMOTE,
    PREFER_SERVICE,
    REASON_OVER_AGE,
    REQUIRED_POLICY_KEYWORDS,
    SOURCE_CACHE,
    SOURCE_EXTRA,
    SOURCE_SERVICE,
    STRICTLY_FRESHER,
    UNAVAILABLE_INVALID_LOCAL,
    UNAVAILABLE_NOT_ATTEMPTED,
    UNAVAILABLE_OFFLINE,
    EventLog,
    ForbiddenEndpoint,
    StubEndpoint,
    extra_frame,
    forbid_acquisition,
    forbid_cache,
    forbid_file_reads,
    frame_of,
    policy,
    resolve_module,
    resolve_orbits,
)


OBS = jd(2023, 2, 21)

A, B, C, D, E = 25544, 38833, 43013, 26867, 48274


def _cache(tmp_path) -> TextOrbitCache:
    """A cache of this test's own. No test ever reads a real user cache."""
    return TextOrbitCache(tmp_path / "cache")


def _endpoint(answers=None, label="nearest-TLE", **kwargs) -> StubEndpoint:
    return StubEndpoint(label, answers=answers, **kwargs)


# ---------------------------------------------------------------------------
# The interface itself: nothing is defaulted
# ---------------------------------------------------------------------------

def test_policy_arguments_are_required(tmp_path, monkeypatch):
    """Every selection and acquisition rule has to be stated by the caller.

    A default here would be an application's policy installed in a shared
    library: the value one consumer happens to use today, silently applied to
    every other caller that did not think about it. So each keyword is required,
    and omitting one is a ``TypeError`` — before anything is read or requested.
    """
    forbid_acquisition(monkeypatch)
    forbid_cache(monkeypatch)
    forbid_file_reads(monkeypatch)

    signature = inspect.signature(resolve_module().resolve_orbits)
    parameters = signature.parameters
    assert list(parameters)[:2] == ["norad_ids", "obs_epoch_jd"]
    for name in REQUIRED_POLICY_KEYWORDS:
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert parameters[name].default is inspect.Parameter.empty, name
    # The resolver never opens a directory: the caller chooses its own ingestion
    # contract and hands over records it has already read.
    assert "extra_orbit_dir" not in parameters
    assert parameters["extra_records"].default is None
    assert parameters["on_event"].default is None
    assert parameters["log"].default is print

    for omitted in REQUIRED_POLICY_KEYWORDS:
        settings = policy()
        del settings[omitted]
        with pytest.raises(TypeError, match=omitted):
            resolve_orbits([A], OBS, log=lambda _m: None, **settings)

    with pytest.raises(TypeError):
        resolve_orbits([A], log=lambda _m: None, **policy())


@pytest.mark.parametrize(
    "overrides,names",
    [
        ({"remote_max_age_days": -1.0}, "remote_max_age_days"),
        ({"remote_max_age_days": float("nan")}, "remote_max_age_days"),
        ({"remote_max_age_days": float("inf")}, "remote_max_age_days"),
        ({"remote_max_age_days": True}, "remote_max_age_days"),
        ({"cache_reuse_max_age_days": -0.5}, "cache_reuse_max_age_days"),
        ({"cache_reuse_max_age_days": False}, "cache_reuse_max_age_days"),
        ({"extra_orbit_max_age_days": -1.0}, "extra_orbit_max_age_days"),
        ({"extra_orbit_max_age_days": float("nan")}, "extra_orbit_max_age_days"),
        # A reuse threshold above the hard ceiling suppresses the request that
        # could have replaced the record the ceiling then rejects: no record, and
        # no attempt to get one.
        (
            {"remote_max_age_days": 3.0, "cache_reuse_max_age_days": 5.0},
            "cache_reuse_max_age_days",
        ),
        ({"offline": "yes"}, "offline"),
        ({"offline": 1}, "offline"),
        ({"allow_missing_checksum": 1}, "allow_missing_checksum"),
        ({"strict_response": 0}, "strict_response"),
        ({"fallback": "true"}, "fallback"),
        ({"max_workers": 0}, "max_workers"),
        ({"max_workers": -2}, "max_workers"),
        ({"max_workers": True}, "max_workers"),
        ({"max_workers": 2.5}, "max_workers"),
        ({"source_order": ()}, "source_order"),
        ({"source_order": (GROUP_EXTRA, GROUP_EXTRA)}, "source_order"),
        ({"source_order": (GROUP_REMOTE, "elsewhere")}, "source_order"),
        ({"source_order": GROUP_EXTRA}, "source_order"),
        ({"replacement": "newest"}, "replacement"),
        ({"replacement": None}, "replacement"),
        ({"endpoints": ()}, "endpoints"),
    ],
)
def test_invalid_policy_fails_before_io(monkeypatch, overrides, names):
    """A malformed rule is a caller error, raised before anything is touched."""
    forbid_acquisition(monkeypatch)
    forbid_cache(monkeypatch)
    forbid_file_reads(monkeypatch)

    with pytest.raises(ValueError, match=names):
        resolve_orbits([A], OBS, log=lambda _m: None, **policy(**overrides))


def test_duplicate_endpoint_labels_are_refused(monkeypatch):
    """Two endpoints under one label make every attempt record ambiguous."""
    forbid_acquisition(monkeypatch)
    forbid_cache(monkeypatch)
    endpoints = (_endpoint(label="same").pair, _endpoint(label="same").pair)
    with pytest.raises(ValueError, match="endpoints"):
        resolve_orbits(
            [A], OBS, log=lambda _m: None, **policy(endpoints=endpoints)
        )


# ---------------------------------------------------------------------------
# Requested identities
# ---------------------------------------------------------------------------

def test_ids_are_exact_and_deduplicated_in_order():
    """IDs are normalised exactly, kept in first-occurrence order, asked for once."""
    endpoint = _endpoint()
    resolution = resolve_orbits(
        [B, "25544.0", B],
        OBS,
        log=lambda _m: None,
        **policy(source_order=(GROUP_REMOTE,), endpoints=(endpoint.pair,)),
    )
    assert resolution.requested == [B, A]
    assert sorted(endpoint.requested) == sorted([A, B])
    assert len(endpoint.requested) == 2

    numpy_endpoint = _endpoint()
    numpy_resolution = resolve_orbits(
        iter([np.int64(B), np.int32(A)]),
        OBS,
        log=lambda _m: None,
        **policy(source_order=(GROUP_REMOTE,), endpoints=(numpy_endpoint.pair,)),
    )
    assert numpy_resolution.requested == [B, A]


@pytest.mark.parametrize(
    "norad_ids",
    [
        "25544",
        b"25544",
        [25544.5],
        ["25544.5"],
        [0],
        [-1],
        [float("nan")],
        [float("inf")],
        [True],
        [np.bool_(True)],
        ["not-an-id"],
        [None],
    ],
)
def test_malformed_requested_ids_are_refused(monkeypatch, norad_ids):
    forbid_acquisition(monkeypatch)
    forbid_cache(monkeypatch)
    with pytest.raises(ValueError):
        resolve_orbits(norad_ids, OBS, log=lambda _m: None, **policy())


@pytest.mark.parametrize("norad_ids", [[], None, ()])
def test_empty_request_touches_no_source(monkeypatch, norad_ids):
    """Nothing requested means nothing read, nothing asked and nothing written."""
    forbid_acquisition(monkeypatch)
    forbid_cache(monkeypatch)
    forbid_file_reads(monkeypatch)

    resolution = resolve_orbits(
        norad_ids, OBS, log=lambda _m: None, **policy()
    )

    assert resolution.requested == []
    assert resolution.resolved == {}
    assert resolution.rejected == {}
    assert resolution.service_errors == {}
    assert resolution.refresh_errors == {}
    assert resolution.unavailable == {}
    assert dict(resolution.attempts) == {}
    assert resolution.missing == []
    assert resolution.complete
    assert resolution.norad_ids() == []
    assert resolution.records() == []
    assert resolution.frame().empty


@pytest.mark.parametrize(
    "epoch", [jd(2000, 3, 4), jd(2010, 6, 15), jd(2017, 5, 14), jd(2019, 3, 2)]
)
def test_observation_epoch_is_required_finite_and_forwarded_unchanged(epoch):
    """The epoch asked about is the epoch requested — never now, never a hint."""
    endpoint = _endpoint()
    resolution = resolve_orbits(
        [A],
        epoch,
        log=lambda _m: None,
        **policy(source_order=(GROUP_REMOTE,), endpoints=(endpoint.pair,)),
    )
    assert resolution.obs_epoch_jd == epoch
    assert endpoint.calls == [(A, epoch)]


@pytest.mark.parametrize("epoch", [float("nan"), float("inf"), -float("inf"), "soon"])
def test_a_non_finite_observation_epoch_is_refused_before_io(monkeypatch, epoch):
    forbid_acquisition(monkeypatch)
    forbid_cache(monkeypatch)
    with pytest.raises(ValueError, match="epoch"):
        resolve_orbits([A], epoch, log=lambda _m: None, **policy())


# ---------------------------------------------------------------------------
# Source precedence
# ---------------------------------------------------------------------------

def _precedence_setup():
    extra = extra_frame([(KIND_TLE, A, OBS - 2.0)])
    endpoint = _endpoint(
        {
            A: frame_of(KIND_TLE, [(A, OBS - 0.5)]),
            B: frame_of(KIND_TLE, [(B, OBS - 0.5)]),
        }
    )
    return extra, endpoint


def test_source_order_controls_precedence_per_id():
    """The first group that resolves an ID wins, decided per ID, not per run."""
    extra, endpoint = _precedence_setup()
    extra_first = resolve_orbits(
        [A, B],
        OBS,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_EXTRA, GROUP_REMOTE), endpoints=(endpoint.pair,)
        ),
        extra_records=extra,
    )
    assert extra_first.resolved[A].source == SOURCE_EXTRA
    assert extra_first.resolved[A].age_days == pytest.approx(2.0, abs=1e-6)
    # An acceptable explicit record wins outright: the service is never asked
    # about that satellite at all.
    assert endpoint.requested == [B]
    assert extra_first.resolved[B].source == SOURCE_SERVICE

    extra, endpoint = _precedence_setup()
    remote_first = resolve_orbits(
        [A, B],
        OBS,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_REMOTE, GROUP_EXTRA), endpoints=(endpoint.pair,)
        ),
        extra_records=extra,
    )
    assert remote_first.resolved[A].source == SOURCE_SERVICE
    assert remote_first.resolved[A].age_days == pytest.approx(0.5, abs=1e-6)
    assert sorted(endpoint.requested) == sorted([A, B])


def test_a_single_source_group_consults_only_that_group():
    extra, _ = _precedence_setup()
    only_extra = resolve_orbits(
        [A, B],
        OBS,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_EXTRA,),
            endpoints=(ForbiddenEndpoint().pair,),
        ),
        extra_records=extra,
    )
    assert only_extra.resolved[A].source == SOURCE_EXTRA
    assert only_extra.missing == [B]
    # Never "SatChecker has no record": nothing asked it.
    assert only_extra.unavailable[B] == UNAVAILABLE_NOT_ATTEMPTED

    extra, endpoint = _precedence_setup()
    only_remote = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        **policy(source_order=(GROUP_REMOTE,), endpoints=(endpoint.pair,)),
        extra_records=extra,
    )
    assert only_remote.resolved[A].source == SOURCE_SERVICE


def test_extra_age_is_independent_of_remote_ceiling():
    """Your own records are yours: the service's ceiling never applies to them."""
    extra = extra_frame([(KIND_TLE, A, OBS - 10.0)])
    unlimited_endpoint = _endpoint()
    unlimited = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        **policy(
            extra_orbit_max_age_days=None,
            remote_max_age_days=0.1,
            cache_reuse_max_age_days=0.1,
            endpoints=(unlimited_endpoint.pair,),
        ),
        extra_records=extra,
    )
    assert unlimited.resolved[A].source == SOURCE_EXTRA
    assert unlimited_endpoint.requested == []

    limited_endpoint = _endpoint()
    limited = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        **policy(
            extra_orbit_max_age_days=1.0,
            remote_max_age_days=0.1,
            cache_reuse_max_age_days=0.1,
            endpoints=(limited_endpoint.pair,),
        ),
        extra_records=extra,
    )
    rejection = limited.rejected[A]
    assert rejection.source == SOURCE_EXTRA
    assert rejection.reason_code == REASON_OVER_AGE
    assert rejection.limit_name == "extra_orbit_max_age_days"
    assert rejection.ceiling_days == 1.0
    assert rejection.age_days == pytest.approx(10.0, abs=1e-6)
    # Rejected by its own ceiling, so the next group gets its turn.
    assert limited_endpoint.requested == [A]


# ---------------------------------------------------------------------------
# The remote ceiling
# ---------------------------------------------------------------------------

@both_kinds
def test_remote_ceiling_applies_to_cache_and_service(tmp_path, kind):
    """One ceiling, measured as a signed offset, for the cache and the service."""
    cache = _cache(tmp_path)
    cache.store(A, frame_of(kind, [(A, OBS - 1.0)]))
    cache.store(B, frame_of(kind, [(B, OBS - 3.0)]))
    cache.store(C, frame_of(kind, [(C, OBS - 5.0)]))
    endpoint = _endpoint(
        {
            D: frame_of(kind, [(D, OBS + 2.0)]),
            E: frame_of(kind, [(E, OBS + 5.0)]),
        },
        label="nearest",
    )

    resolution = resolve_orbits(
        [A, B, C, D, E],
        OBS,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_REMOTE,),
            remote_max_age_days=3.0,
            cache_reuse_max_age_days=1.0,
            cache=cache,
            endpoints=(endpoint.pair,),
        ),
    )

    assert resolution.resolved[A].source == SOURCE_CACHE
    assert resolution.resolved[A].offset_days == pytest.approx(-1.0, abs=1e-6)
    assert resolution.resolved[B].source == SOURCE_CACHE
    assert resolution.resolved[B].offset_days == pytest.approx(-3.0, abs=1e-6)
    assert resolution.resolved[D].source == SOURCE_SERVICE
    assert resolution.resolved[D].endpoint == "nearest"
    assert resolution.resolved[D].offset_days == pytest.approx(2.0, abs=1e-6)

    over_cache = resolution.rejected[C]
    assert over_cache.source == SOURCE_CACHE
    assert over_cache.reason_code == REASON_OVER_AGE
    assert over_cache.limit_name == "remote_max_age_days"
    assert over_cache.ceiling_days == 3.0
    assert over_cache.offset_days == pytest.approx(-5.0, abs=1e-6)

    over_service = resolution.rejected[E]
    assert over_service.source == SOURCE_SERVICE
    assert over_service.endpoint == "nearest"
    assert over_service.reason_code == REASON_OVER_AGE
    assert over_service.limit_name == "remote_max_age_days"
    assert over_service.offset_days == pytest.approx(5.0, abs=1e-6)

    assert resolution.missing == [C, E]


@both_kinds
def test_a_null_remote_ceiling_accepts_every_finite_valid_age(tmp_path, kind):
    cache = _cache(tmp_path)
    cache.store(C, frame_of(kind, [(C, OBS - 500.0)]))
    endpoint = _endpoint({E: frame_of(kind, [(E, OBS + 500.0)])})

    resolution = resolve_orbits(
        [C, E],
        OBS,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_REMOTE,),
            remote_max_age_days=None,
            cache_reuse_max_age_days=None,
            cache=cache,
            endpoints=(endpoint.pair,),
        ),
    )
    assert resolution.complete
    assert resolution.rejected == {}


@pytest.mark.parametrize("ceiling", ["extra", "remote", "reuse"])
@pytest.mark.parametrize(
    "delta,within",
    [
        (0.0, True),
        (2 * AGE_TOL_DAYS / 3, True),
        (100 * AGE_TOL_DAYS, False),
    ],
)
def test_age_tolerance_and_zero_limit(tmp_path, ceiling, delta, within):
    """A zero ceiling means "this epoch", to the precision an epoch is written in.

    A TLE line-1 epoch is quantised to about 0.9 ms and the Julian-date round
    trip adds a little more, so an exact-match record is never bit-exact. The
    tolerance covers exactly that and no more: a record a few milliseconds away
    is still rejected by a zero ceiling.
    """
    line1, _ = make_tle(A, OBS)
    epoch = tle_epoch_jd(line1)
    obs = epoch + delta
    record_frame = frame_of(KIND_TLE, [(A, OBS)])

    if ceiling == "extra":
        resolution = resolve_orbits(
            [A],
            obs,
            log=lambda _m: None,
            **policy(
                source_order=(GROUP_EXTRA,),
                extra_orbit_max_age_days=0.0,
                endpoints=(ForbiddenEndpoint().pair,),
            ),
            extra_records=record_frame,
        )
        assert (A in resolution.resolved) is within
        return

    cache = _cache(tmp_path)
    cache.store(A, record_frame)
    endpoint = _endpoint()
    limits = (
        {"remote_max_age_days": 0.0, "cache_reuse_max_age_days": 0.0}
        if ceiling == "remote"
        else {"remote_max_age_days": 3.0, "cache_reuse_max_age_days": 0.0}
    )
    resolution = resolve_orbits(
        [A],
        obs,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_REMOTE,),
            cache=cache,
            endpoints=(endpoint.pair,),
            **limits,
        ),
    )
    if ceiling == "remote":
        assert (A in resolution.resolved) is within
    else:
        # Under the reuse ceiling the record is acceptable either way; what the
        # tolerance decides is whether a request goes out for a closer one.
        assert A in resolution.resolved
        assert (endpoint.requested == []) is within


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------

def test_nearest_valid_candidate_uses_position_not_index():
    """Nearest *valid* candidate, chosen by position, with ties going to the first."""
    line1, line2 = make_tle(A, OBS - 0.05)
    corrupt = make_tle_record(
        A, OBS - 0.05, TLE_LINE1=line1[:68] + str((int(line1[68]) + 1) % 10)
    )
    nearest_valid = make_omm(A, OBS - 0.5)
    farther = make_tle_record(A, OBS - 3.0)

    # A provider's own derived epoch, deliberately contradicting the lines.
    misleading = make_tle_record(B, OBS - 3.0, EPOCH_JD=OBS, EPOCH="see EPOCH_JD")
    truthful = make_tle_record(B, OBS - 1.0)

    # An *exact* tie: one epoch, two providers. Two records a day either side of
    # the observation are not a tie — their epochs are quantised independently —
    # and would test rounding rather than the rule.
    tie_first = make_tle_record(C, OBS - 1.0, DATA_SOURCE="first")
    tie_second = make_tle_record(C, OBS - 1.0, DATA_SOURCE="second")

    frames = [
        pd.DataFrame([row])
        for row in (corrupt, nearest_valid, farther, misleading, truthful, tie_first, tie_second)
    ]
    # Concatenated without ignore_index, as a caller's own reads routinely are:
    # every row carries label 0, so anything selecting by label selects wrongly.
    extra = pd.concat(frames)
    assert set(extra.index) == {0}

    logged = []
    resolution = resolve_orbits(
        [A, B, C],
        OBS,
        log=logged.append,
        **policy(
            source_order=(GROUP_EXTRA,),
            extra_orbit_max_age_days=None,
            endpoints=(ForbiddenEndpoint().pair,),
        ),
        extra_records=extra,
    )

    assert resolution.resolved[A].offset_days == pytest.approx(-0.5, abs=1e-6)
    assert resolution.resolved[A].record["RECORD_KIND"] == KIND_OMM
    assert resolution.resolved[B].offset_days == pytest.approx(-1.0, abs=1e-6)
    # An exact tie keeps the first input position rather than the later record.
    assert resolution.resolved[C].record["DATA_SOURCE"] == "first"

    frame = resolution.frame()
    assert frame["NORAD_CAT_ID"].tolist() == [A, B, C]
    assert not frame.columns.duplicated().any()


def test_tle_age_uses_line_epoch():
    """A TLE's age comes from line 1. A provider's own epoch field is not evidence."""
    stale_lines = make_tle_record(A, OBS - 10.0, EPOCH=make_omm(A, OBS)["EPOCH"], EPOCH_JD=OBS)
    fresh_lines = make_tle_record(B, OBS - 1.0, EPOCH=make_omm(B, OBS - 10.0)["EPOCH"])

    resolution = resolve_orbits(
        [A, B],
        OBS,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_EXTRA,),
            extra_orbit_max_age_days=3.0,
            endpoints=(ForbiddenEndpoint().pair,),
        ),
        extra_records=pd.DataFrame([stale_lines, fresh_lines]),
    )

    assert A in resolution.rejected
    assert resolution.rejected[A].age_days == pytest.approx(10.0, abs=1e-6)
    assert resolution.resolved[B].age_days == pytest.approx(1.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Cache reuse versus the hard ceiling
# ---------------------------------------------------------------------------

def test_cache_reuse_requires_an_accepted_incumbent(tmp_path):
    """Reuse suppresses a request only for a record the ceiling actually accepts.

    Without the intersection, ``cache_reuse_max_age_days: null`` makes every
    cached record a hit — including ones the hard ceiling then rejects — and the
    satellite is never fetched at all.
    """
    cache = _cache(tmp_path)
    cache.store(A, frame_of(KIND_TLE, [(A, OBS - 0.5)]))
    cache.store(B, frame_of(KIND_TLE, [(B, OBS - 2.0)]))
    cache.store(C, frame_of(KIND_TLE, [(C, OBS - 4.0)]))

    endpoint = _endpoint()
    resolve_orbits(
        [A, B, C],
        OBS,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_REMOTE,),
            remote_max_age_days=3.0,
            cache_reuse_max_age_days=1.0,
            cache=cache,
            endpoints=(endpoint.pair,),
        ),
    )
    assert sorted(endpoint.requested) == [B, C]

    unlimited = _endpoint()
    resolve_orbits(
        [A, B, C],
        OBS,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_REMOTE,),
            remote_max_age_days=3.0,
            cache_reuse_max_age_days=None,
            cache=cache,
            endpoints=(unlimited.pair,),
        ),
    )
    assert unlimited.requested == [C]


def test_zero_reuse_only_suppresses_epoch_matching_cache(tmp_path):
    line1, _ = make_tle(A, OBS)
    exact = tle_epoch_jd(line1)
    cache = _cache(tmp_path)
    cache.store(A, frame_of(KIND_TLE, [(A, OBS)]))
    cache.store(B, frame_of(KIND_TLE, [(B, OBS - 0.5)]))

    endpoint = _endpoint()
    resolve_orbits(
        [A, B],
        exact,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_REMOTE,),
            remote_max_age_days=3.0,
            cache_reuse_max_age_days=0.0,
            cache=cache,
            endpoints=(endpoint.pair,),
        ),
    )
    assert endpoint.requested == [B]


# ---------------------------------------------------------------------------
# Incumbent replacement
# ---------------------------------------------------------------------------

def _replacement_setup(tmp_path):
    """One incumbent age, four service answers: closer, equal, staler, over-age.

    The "equal" answer carries the incumbent's *own* epoch rather than a
    mirrored one, because two epochs a fixed interval either side of the
    observation are quantised independently and are not exactly equidistant.
    The providers differ, so which record won is still visible.
    """
    cache = _cache(tmp_path)
    for norad_id in (A, B, C, D):
        cache.store(
            norad_id, frame_of(KIND_TLE, [(norad_id, OBS - 2.0)], DATA_SOURCE="cached")
        )
    endpoint = _endpoint(
        {
            A: frame_of(KIND_TLE, [(A, OBS - 1.0)], DATA_SOURCE="served"),
            B: frame_of(KIND_TLE, [(B, OBS - 2.0)], DATA_SOURCE="served"),
            C: frame_of(KIND_TLE, [(C, OBS - 2.5)], DATA_SOURCE="served"),
            D: frame_of(KIND_TLE, [(D, OBS - 4.0)], DATA_SOURCE="served"),
        }
    )
    return cache, endpoint


def test_strictly_fresher_retains_equal_and_closer_incumbents(tmp_path):
    """Only a strictly closer record displaces one already held.

    That makes a refresh safe by construction: a staler or equally distant
    answer — and a failed one — leaves the run with what it already had.
    """
    cache, endpoint = _replacement_setup(tmp_path)
    resolution = resolve_orbits(
        [A, B, C, D],
        OBS,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_REMOTE,),
            replacement=STRICTLY_FRESHER,
            remote_max_age_days=3.0,
            cache_reuse_max_age_days=0.0,
            cache=cache,
            endpoints=(endpoint.pair,),
        ),
    )
    assert sorted(endpoint.requested) == sorted([A, B, C, D])
    assert resolution.resolved[A].source == SOURCE_SERVICE
    assert resolution.resolved[A].provider == "served"
    assert resolution.resolved[A].offset_days == pytest.approx(-1.0, abs=1e-6)
    for norad_id in (B, C, D):
        assert resolution.resolved[norad_id].source == SOURCE_CACHE, norad_id
        assert resolution.resolved[norad_id].provider == "cached", norad_id
        assert resolution.resolved[norad_id].offset_days == pytest.approx(
            -2.0, abs=1e-6
        )


def test_prefer_service_changes_only_acceptable_replacement(tmp_path):
    cache, endpoint = _replacement_setup(tmp_path)
    resolution = resolve_orbits(
        [A, B, C, D],
        OBS,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_REMOTE,),
            replacement=PREFER_SERVICE,
            remote_max_age_days=3.0,
            cache_reuse_max_age_days=0.0,
            cache=cache,
            endpoints=(endpoint.pair,),
        ),
    )
    assert resolution.resolved[A].source == SOURCE_SERVICE
    assert resolution.resolved[B].source == SOURCE_SERVICE
    assert resolution.resolved[B].provider == "served"
    assert resolution.resolved[C].source == SOURCE_SERVICE
    assert resolution.resolved[C].offset_days == pytest.approx(-2.5, abs=1e-6)
    # Over the ceiling is over the ceiling under either replacement rule.
    assert resolution.resolved[D].source == SOURCE_CACHE
    assert resolution.resolved[D].provider == "cached"


# ---------------------------------------------------------------------------
# Offline, and a cache that is not there
# ---------------------------------------------------------------------------

def test_offline_keeps_ceiling_and_skips_refresh(tmp_path, monkeypatch):
    """Offline is about what can be reached, not about what is acceptable."""
    cache = _cache(tmp_path)
    cache.store(A, frame_of(KIND_TLE, [(A, OBS - 2.0)]))
    cache.store(B, frame_of(KIND_TLE, [(B, OBS - 5.0)]))
    forbid_acquisition(monkeypatch)

    resolution = resolve_orbits(
        [A, B, C],
        OBS,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_REMOTE,),
            offline=True,
            remote_max_age_days=3.0,
            cache_reuse_max_age_days=1.0,
            cache=cache,
            endpoints=(ForbiddenEndpoint().pair,),
        ),
    )

    assert resolution.offline is True
    assert resolution.resolved[A].source == SOURCE_CACHE
    # The age rejection and the offline classification are both true and both
    # kept: flattening them loses which one the user can do something about.
    assert resolution.rejected[B].reason_code == REASON_OVER_AGE
    assert resolution.unavailable[B] == UNAVAILABLE_OFFLINE
    assert C not in resolution.rejected
    assert resolution.unavailable[C] == UNAVAILABLE_OFFLINE
    assert resolution.service_errors == {}


def _corrupted(frame) -> pd.DataFrame:
    """*frame*'s first TLE with one checksum digit wrong, so it will not validate."""
    frame = frame.copy()
    line1 = frame.loc[0, "TLE_LINE1"]
    frame.loc[0, "TLE_LINE1"] = line1[:68] + str((int(line1[68]) + 1) % 10)
    return frame


def test_offline_survives_an_unusable_record_beside_an_over_age_one(tmp_path, monkeypatch):
    """``invalid_local`` means *only* unusable local evidence, and this is not that.

    A too-old cached record is measurable evidence, and the ceiling that
    refused it is a remedy; saying the run had nothing but a broken file hides
    both, and hides that nothing was allowed to ask for something better.
    """
    cache = _cache(tmp_path)
    cache.store(A, frame_of(KIND_TLE, [(A, OBS - 5.0)]))
    forbid_acquisition(monkeypatch)
    corrupt = _corrupted(extra_frame([(KIND_TLE, A, OBS - 0.5)]))

    settings = dict(
        source_order=(GROUP_EXTRA, GROUP_REMOTE),
        offline=True,
        remote_max_age_days=3.0,
        extra_orbit_max_age_days=None,
        endpoints=(ForbiddenEndpoint().pair,),
    )
    resolution = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        **policy(cache=cache, **settings),
        extra_records=corrupt,
    )

    assert resolution.rejected[A].source == SOURCE_CACHE
    assert resolution.rejected[A].reason_code == REASON_OVER_AGE
    assert resolution.unavailable[A] == UNAVAILABLE_OFFLINE

    # With nothing but the broken file, the evidence *is* exclusively unusable.
    only_invalid = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        **policy(cache=None, **settings),
        extra_records=corrupt,
    )
    assert only_invalid.unavailable[A] == UNAVAILABLE_INVALID_LOCAL


def test_cache_none_disables_storage(monkeypatch):
    """``cache=None`` means no reads and no writes, and no cache discovered either."""
    forbid_cache(monkeypatch)
    endpoint = _endpoint({A: frame_of(KIND_TLE, [(A, OBS - 0.5)])})

    resolution = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_REMOTE,), cache=None, endpoints=(endpoint.pair,)
        ),
    )
    assert resolution.resolved[A].source == SOURCE_SERVICE


def test_fetched_history_is_cached_before_age_selection(tmp_path):
    """Everything verifiable that came back is kept; selection happens after.

    A record rejected on age for this observation is the record a run at another
    epoch wants, and it has already been paid for.
    """
    cache = _cache(tmp_path)
    history = make_catalogue_df([(A, OBS - 0.5), (A, OBS - 4.0), (A, OBS + 6.0)])
    endpoint = _endpoint({A: history})

    resolution = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_REMOTE,),
            remote_max_age_days=3.0,
            cache=cache,
            endpoints=(endpoint.pair,),
        ),
    )

    assert resolution.resolved[A].offset_days == pytest.approx(-0.5, abs=1e-6)
    assert len(cache.get(A, log=lambda _m: None)) == 3


def test_cache_write_failure_preserves_resolution(tmp_path, monkeypatch):
    """A cache that cannot be written loses the reuse, never the fetched record."""
    cache = _cache(tmp_path)

    def failing_store(norad_id, records):
        raise OSError("disk full")

    monkeypatch.setattr(cache, "store", failing_store)
    endpoint = _endpoint({A: frame_of(KIND_TLE, [(A, OBS - 0.5)])})
    logged = []
    events = EventLog()

    resolution = resolve_orbits(
        [A],
        OBS,
        log=logged.append,
        on_event=events,
        **policy(
            source_order=(GROUP_REMOTE,), cache=cache, endpoints=(endpoint.pair,)
        ),
    )

    assert resolution.resolved[A].source == SOURCE_SERVICE
    assert resolution.service_errors == {}
    assert any("could not write" in line and "disk full" in line for line in logged)
    (failure,) = events.of(EVENT_CACHE_WRITE_FAILED)
    assert failure.path is not None and "disk full" in str(failure.error)


# ---------------------------------------------------------------------------
# The result
# ---------------------------------------------------------------------------

def test_result_frame_is_ordered_derived_and_nonmutating():
    """One row per accepted ID, in requested order, with elements derived here."""
    tle = make_tle_record(A, OBS - 0.5, EPOCH_JD=0.0, SEMIMAJOR_AXIS=1.0)
    omm = make_omm(B, OBS - 0.5, EPOCH_JD=0.0, SEMIMAJOR_AXIS=1.0)
    extra = pd.DataFrame([omm, tle])
    before = extra.copy(deep=True)

    resolution = resolve_orbits(
        [A, B],
        OBS,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_EXTRA,),
            extra_orbit_max_age_days=None,
            endpoints=(ForbiddenEndpoint().pair,),
        ),
        extra_records=extra,
    )

    frame = resolution.frame()
    assert frame["NORAD_CAT_ID"].tolist() == [A, B]
    assert all(isinstance(value, (int, np.integer)) for value in frame["NORAD_CAT_ID"])
    assert not frame.columns.duplicated().any()
    assert resolution.norad_ids() == [A, B]

    for position, norad_id in enumerate([A, B]):
        record = resolution.resolved[norad_id].record
        expected = record_elements(record)
        for column, value in expected.items():
            # Exactly, not approximately: the frame's elements must be the ones
            # derived here, not a provider's stale copy of them.
            assert frame.loc[position, column] == value, (norad_id, column)
        assert frame.loc[position, "EPOCH_JD"] == record_epoch_jd(record)

    # The caller's frame is input, not scratch space.
    pd.testing.assert_frame_equal(extra, before)

    first = resolution.records()
    first[0]["OBJECT_NAME"] = "mutated"
    assert resolution.records()[0]["OBJECT_NAME"] != "mutated"
    assert resolution.resolved[A].record["OBJECT_NAME"] != "mutated"


def test_attempts_are_recorded_in_endpoint_order(tmp_path):
    """Per ID, one attempt per configured endpoint, in the order they were offered."""
    first = _endpoint({A: frame_of(KIND_TLE, [(A, OBS - 0.5)])}, label="first")
    second = _endpoint(label="second")

    resolution = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_REMOTE,), endpoints=(first.pair, second.pair)
        ),
    )

    attempts = list(resolution.attempts[A])
    assert [attempt.endpoint for attempt in attempts] == ["first", "second"]
    assert attempts[0].status == ATTEMPT_USABLE
    # An endpoint that was never needed says so, rather than being absent: the
    # difference between "asked and had nothing" and "not asked" is the whole
    # point of keeping attempts.
    assert attempts[1].status == ATTEMPT_NOT_SENT
    assert second.requested == []


def test_the_result_states_the_policy_it_was_run_under():
    resolution = resolve_orbits(
        [],
        OBS,
        log=lambda _m: None,
        **policy(
            remote_max_age_days=2.5,
            cache_reuse_max_age_days=0.25,
            extra_orbit_max_age_days=7.0,
            offline=True,
        ),
    )
    assert resolution.remote_max_age_days == 2.5
    assert resolution.cache_reuse_max_age_days == 0.25
    assert resolution.extra_orbit_max_age_days == 7.0
    assert resolution.offline is True
    assert math.isfinite(resolution.obs_epoch_jd)
