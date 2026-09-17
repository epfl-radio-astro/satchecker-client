"""Offline tests for the resolver's acquisition: endpoints, fallback, outcomes.

The distinction every test here defends is the one that turns a failure into a
plausible-looking result: *the service has no record for this satellite* versus
*we could not find out*. The first legitimately excludes a satellite; the second
must never be reported as the first, because a simulation missing an RFI source
it was asked for is indistinguishable from a correct simulation of a quiet sky.

Absence, an over-age answer, a failed request and an outage that prevented the
request are therefore four different things in the result, and the endpoint
fallback has to be driven by what an endpoint *supplied*, not by what is already
held.
"""

import json
import threading
import time

import pandas as pd
import pytest

from satchecker_client import client, service
from satchecker_client.cache import TextOrbitCache
from satchecker_client.client import (
    HANDOVER_JD,
    SatCheckerRateLimitError,
    SatCheckerResponseError,
    SatCheckerTransportError,
)
from satchecker_client.records import (
    CHECKSUM_STATUS_FIELD,
    CHECKSUM_UNVERIFIED_MISSING,
    KIND_TLE,
)
from satchecker_client.service import (
    RESPONSE_WALL_THRESHOLD,
    NearestBatchResult,
    nearest_endpoints_for,
)

from .tle_helpers import (  # noqa: F401  block_network is an autouse fixture
    block_network,
    jd,
    make_catalogue_df,
)
from .resolve_helpers import (
    ATTEMPT_EMPTY,
    ATTEMPT_ERROR,
    ATTEMPT_NOT_SENT,
    ATTEMPT_OVER_AGE,
    ATTEMPT_USABLE,
    EVENT_CANDIDATE_REJECTED,
    EVENT_ENDPOINT_FALLBACK,
    EVENT_OUTAGE,
    EVENT_REFRESH_FAILED,
    GROUP_EXTRA,
    GROUP_REMOTE,
    REASON_INVALID,
    REASON_OVER_AGE,
    SOURCE_CACHE,
    SOURCE_EXTRA,
    SOURCE_SERVICE,
    UNAVAILABLE_ABSENT,
    UNAVAILABLE_INVALID_LOCAL,
    UNAVAILABLE_OFFLINE,
    EventLog,
    ForbiddenEndpoint,
    RejectedOrbit,
    StubEndpoint,
    extra_frame,
    frame_of,
    policy,
    resolve_orbits,
)


OBS = jd(2023, 2, 21)

A, B, C, D, E, F = 25544, 38833, 43013, 26867, 48274, 20580


def _quiet(**overrides) -> dict:
    return policy(source_order=(GROUP_REMOTE,), **overrides)


def _corrupt_frame(norad_id, epoch_jd) -> pd.DataFrame:
    """A reply whose record does not validate, so it is a failure, not absence."""
    frame = make_catalogue_df([(norad_id, epoch_jd)])
    line1 = frame.loc[0, "TLE_LINE1"]
    frame.loc[0, "TLE_LINE1"] = line1[:68] + str((int(line1[68]) + 1) % 10)
    return frame


# ---------------------------------------------------------------------------
# Which archive is asked, and in what order
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "epoch,expected",
    [
        (HANDOVER_JD - 1.0, ["nearest-TLE", "nearest-OMM"]),
        (HANDOVER_JD, ["nearest-OMM", "nearest-TLE"]),
        (HANDOVER_JD + 1.0, ["nearest-OMM", "nearest-TLE"]),
    ],
)
def test_handover_endpoint_order(monkeypatch, epoch, expected):
    """The caller supplies the order; the handover helper is where it comes from.

    Patched through ``client``, before ``nearest_endpoints_for`` is called, so
    the endpoint pairs the resolver receives are the real ones.
    """
    journal = []
    monkeypatch.setattr(
        client, "fetch_nearest_tle", StubEndpoint("nearest-TLE", journal=journal)
    )
    monkeypatch.setattr(
        client, "fetch_nearest_omm", StubEndpoint("nearest-OMM", journal=journal)
    )

    resolve_orbits(
        [A],
        epoch,
        log=lambda _m: None,
        **_quiet(endpoints=nearest_endpoints_for(epoch), fallback=True),
    )

    assert [label for label, _ in journal] == expected


def test_explicit_endpoint_order_overrides_handover(monkeypatch):
    journal = []
    monkeypatch.setattr(
        client, "fetch_nearest_tle", StubEndpoint("nearest-TLE", journal=journal)
    )
    monkeypatch.setattr(
        client, "fetch_nearest_omm", StubEndpoint("nearest-OMM", journal=journal)
    )
    reversed_pairs = list(reversed(nearest_endpoints_for(HANDOVER_JD + 1.0)))

    resolve_orbits(
        [A],
        HANDOVER_JD + 1.0,
        log=lambda _m: None,
        **_quiet(endpoints=reversed_pairs, fallback=True),
    )

    assert [label for label, _ in journal] == ["nearest-TLE", "nearest-OMM"]


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "primary,status",
    [
        ("empty", ATTEMPT_EMPTY),
        ("over_age", ATTEMPT_OVER_AGE),
        ("error", ATTEMPT_ERROR),
    ],
)
@pytest.mark.parametrize("fallback", [True, False])
def test_fallback_policy_controls_second_endpoint(primary, status, fallback):
    """Fallback turns on what the first endpoint supplied, and nothing else."""
    answers = {
        "empty": None,
        "over_age": frame_of(KIND_TLE, [(A, OBS - 10.0)]),
        "error": SatCheckerResponseError("malformed"),
    }[primary]
    first = StubEndpoint("first", answers={A: answers})
    second = StubEndpoint("second", answers={A: frame_of(KIND_TLE, [(A, OBS - 0.5)])})

    resolution = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        **_quiet(endpoints=(first.pair, second.pair), fallback=fallback),
    )

    assert first.requested == [A]
    attempts = list(resolution.attempts[A])
    assert attempts[0].status == status

    if fallback:
        assert second.requested == [A]
        assert resolution.resolved[A].source == SOURCE_SERVICE
        assert resolution.resolved[A].endpoint == "second"
        assert attempts[1].status == ATTEMPT_USABLE
        return

    assert second.requested == []
    assert attempts[1].status == ATTEMPT_NOT_SENT
    assert resolution.missing == [A]
    if primary == "empty":
        assert resolution.unavailable[A] == UNAVAILABLE_ABSENT
    elif primary == "over_age":
        assert resolution.rejected[A].reason_code == REASON_OVER_AGE
        assert A not in resolution.unavailable
    else:
        assert isinstance(resolution.service_errors[A], SatCheckerResponseError)
        assert A not in resolution.unavailable


def test_usable_primary_suppresses_fallback_even_without_improvement(tmp_path):
    """An in-ceiling answer is an answer, whether or not it beats the incumbent.

    Asking the other archive because the record we already hold is closer would
    make the fallback a global-nearest search across both archives, which is a
    different acquisition policy and a different request volume.
    """
    cache = TextOrbitCache(tmp_path / "cache")
    cache.store(A, frame_of(KIND_TLE, [(A, OBS - 0.5)]))
    first = StubEndpoint("first", answers={A: frame_of(KIND_TLE, [(A, OBS - 2.0)])})
    second = StubEndpoint("second")

    resolution = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        **_quiet(
            cache=cache,
            cache_reuse_max_age_days=0.1,
            endpoints=(first.pair, second.pair),
            fallback=True,
        ),
    )

    assert first.requested == [A]
    assert second.requested == []
    assert resolution.resolved[A].source == SOURCE_CACHE


def test_stale_cached_incumbent_does_not_suppress_fallback(tmp_path):
    """The incumbent is not evidence about what the first archive holds."""
    cache = TextOrbitCache(tmp_path / "cache")
    cache.store(A, frame_of(KIND_TLE, [(A, OBS - 2.5)]))
    first = StubEndpoint("first", answers={A: frame_of(KIND_TLE, [(A, OBS - 10.0)])})
    second = StubEndpoint("second", answers={A: frame_of(KIND_TLE, [(A, OBS - 0.5)])})
    events = EventLog()

    resolution = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        on_event=events,
        **_quiet(
            cache=cache,
            cache_reuse_max_age_days=1.0,
            endpoints=(first.pair, second.pair),
            fallback=True,
        ),
    )

    assert second.requested == [A]
    assert resolution.resolved[A].source == SOURCE_SERVICE
    assert resolution.resolved[A].endpoint == "second"
    assert events.of(EVENT_ENDPOINT_FALLBACK)


# ---------------------------------------------------------------------------
# Telling the outcomes apart
# ---------------------------------------------------------------------------

def test_absence_age_and_service_failure_are_distinct():
    """Four unresolved satellites, four different reasons, none flattened."""
    over_age = {
        C: frame_of(KIND_TLE, [(C, OBS - 20.0)]),
        D: frame_of(KIND_TLE, [(D, OBS + 20.0)]),
    }
    failures = {
        E: SatCheckerResponseError("malformed reply"),
        F: _corrupt_frame(F, OBS - 0.5),
    }
    first = StubEndpoint("first", answers={**over_age, **failures})
    second = StubEndpoint("second", answers={**over_age, **failures})

    resolution = resolve_orbits(
        [A, B, C, D, E, F],
        OBS,
        log=lambda _m: None,
        **_quiet(endpoints=(first.pair, second.pair), fallback=True),
    )

    assert resolution.missing == [A, B, C, D, E, F]
    # Only a successful, genuinely empty answer from every endpoint the policy
    # needed is absence.
    assert {A: resolution.unavailable.get(A), B: resolution.unavailable.get(B)} == {
        A: UNAVAILABLE_ABSENT,
        B: UNAVAILABLE_ABSENT,
    }
    assert set(resolution.unavailable) == {A, B}
    assert set(resolution.rejected) == {C, D}
    assert resolution.rejected[C].reason_code == REASON_OVER_AGE
    assert resolution.rejected[D].offset_days == pytest.approx(20.0, abs=1e-6)
    assert set(resolution.service_errors) == {E, F}
    assert isinstance(resolution.service_errors[F], SatCheckerResponseError)


@pytest.mark.parametrize("source", [SOURCE_EXTRA, SOURCE_CACHE])
def test_an_over_age_local_record_is_not_absence(tmp_path, source):
    """The archives having nothing does not make a too-old record absent.

    A satellite whose only record is five days from the observation is one the
    user can do something about — widen the ceiling, supply a closer record.
    Reporting it as one the archives have no record of hides the record and
    the remedy at once.
    """
    settings = dict(remote_max_age_days=3.0, extra_orbit_max_age_days=3.0)
    extra_records = None
    if source == SOURCE_EXTRA:
        settings.update(source_order=(GROUP_EXTRA, GROUP_REMOTE))
        extra_records = extra_frame([(KIND_TLE, A, OBS - 5.0)])
    else:
        cache = TextOrbitCache(tmp_path / "cache")
        cache.store(A, frame_of(KIND_TLE, [(A, OBS - 5.0)]))
        settings.update(source_order=(GROUP_REMOTE,), cache=cache)
    first, second = StubEndpoint("first"), StubEndpoint("second")

    resolution = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        **policy(endpoints=(first.pair, second.pair), fallback=True, **settings),
        extra_records=extra_records,
    )

    assert [attempt.status for attempt in resolution.attempts[A]] == [
        ATTEMPT_EMPTY,
        ATTEMPT_EMPTY,
    ]
    assert resolution.rejected[A].source == source
    assert resolution.rejected[A].reason_code == REASON_OVER_AGE
    assert A not in resolution.unavailable


def test_best_rejection_retains_offset_and_its_own_ceiling():
    """The rejection kept is the nearest measurable one, with *its* limit named."""
    extra = extra_frame([(KIND_TLE, A, OBS - 10.0)])
    corrupt = _corrupt_frame(A, OBS - 0.1)
    extra = pd.concat([extra, corrupt], ignore_index=True)

    first = StubEndpoint("first", answers={A: frame_of(KIND_TLE, [(A, OBS - 5.0)])})
    second = StubEndpoint("second", answers={A: _corrupt_frame(A, OBS - 0.2)})
    events = EventLog()

    resolution = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        on_event=events,
        **policy(
            source_order=(GROUP_EXTRA, GROUP_REMOTE),
            extra_orbit_max_age_days=1.0,
            remote_max_age_days=3.0,
            endpoints=(first.pair, second.pair),
            fallback=True,
        ),
        extra_records=extra,
    )

    best = resolution.rejected[A]
    assert best.source == SOURCE_SERVICE
    assert best.endpoint == "first"
    assert best.reason_code == REASON_OVER_AGE
    assert best.limit_name == "remote_max_age_days"
    assert best.ceiling_days == 3.0
    assert best.age_days == pytest.approx(5.0, abs=1e-6)
    # The other evidence is not lost, only not the headline.
    assert any(
        event.source == SOURCE_EXTRA for event in resolution.events if A in event.norad_ids
    )


def _unknown_status_frame(norad_id, epoch_jd) -> pd.DataFrame:
    """A record whose lines are fine and whose provenance claim is not ours.

    Valid enough for the cache's own validator, which reads the lines and not
    the claim, and refused by the resolver — which is how a satellite ends up
    with two unusable candidates that fail for different reasons.
    """
    return frame_of(
        KIND_TLE, [(norad_id, epoch_jd)], **{CHECKSUM_STATUS_FIELD: "probably-fine"}
    )


def test_a_retained_rejection_carries_the_error_that_refused_it(tmp_path):
    """The kept rejection's diagnostic is its own candidate's, not the last one's.

    Two unusable candidates for one satellite, from two sources. The first is
    the rejection kept, so an application reading ``.error`` beside ``.source``
    describes one candidate; matching ``candidate_rejected`` events by ID
    instead would put the cached record's reason against the file's source.
    """
    cache = TextOrbitCache(tmp_path / "cache")
    cache.store(A, _unknown_status_frame(A, OBS - 0.5))
    events = EventLog()

    resolution = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        on_event=events,
        **policy(
            source_order=(GROUP_EXTRA, GROUP_REMOTE),
            extra_orbit_max_age_days=3.0,
            remote_max_age_days=3.0,
            offline=True,
            cache=cache,
        ),
        extra_records=_corrupt_frame(A, OBS - 0.5),
    )

    rejected = events.for_id(A, EVENT_CANDIDATE_REJECTED)
    assert [event.source for event in rejected] == [SOURCE_EXTRA, SOURCE_CACHE]

    rejection = resolution.rejected[A]
    assert rejection.source == SOURCE_EXTRA
    assert rejection.reason_code == REASON_INVALID
    assert rejection.error is rejected[0].error
    # The other candidate's own reason is still reported, just not as this one's.
    assert rejected[1].error is not None
    assert rejected[1].error is not rejection.error


def test_two_unusable_candidates_from_one_source_keep_the_first_error():
    """One source, two defects: the retained rejection is the first candidate's."""
    extra = pd.concat(
        [_corrupt_frame(A, OBS - 0.5), _unknown_status_frame(A, OBS - 0.2)],
        ignore_index=True,
    )
    events = EventLog()

    resolution = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        on_event=events,
        **policy(source_order=(GROUP_EXTRA,), extra_orbit_max_age_days=3.0),
        extra_records=extra,
    )

    rejected = events.for_id(A, EVENT_CANDIDATE_REJECTED)
    assert [event.source for event in rejected] == [SOURCE_EXTRA, SOURCE_EXTRA]
    assert resolution.rejected[A].error is rejected[0].error
    assert rejected[1].error is not rejected[0].error


def test_an_over_age_rejection_carries_no_error():
    """Nothing refused the record: it was read, measured and found too far away."""
    resolution = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        **policy(source_order=(GROUP_EXTRA,), extra_orbit_max_age_days=3.0),
        extra_records=extra_frame([(KIND_TLE, A, OBS - 5.0)]),
    )

    rejection = resolution.rejected[A]
    assert rejection.reason_code == REASON_OVER_AGE
    assert rejection.error is None


def test_rejected_orbit_still_takes_its_existing_positional_arguments():
    """The new field is appended, so every existing construction still builds one."""
    invalid = RejectedOrbit(A, SOURCE_EXTRA, None, None, None, None, REASON_INVALID)
    over_age = RejectedOrbit(
        A,
        SOURCE_CACHE,
        None,
        None,
        OBS - 5.0,
        -5.0,
        REASON_OVER_AGE,
        3.0,
        "remote_max_age_days",
    )
    assert (invalid.error, over_age.error) == (None, None)
    assert over_age.limit_name == "remote_max_age_days"


def test_a_resolver_only_validation_failure_is_a_response_error(monkeypatch):
    """Rows came back, and none of them passed *this* layer's policy.

    The batch validated them against the policy it was given, which does not
    read the provenance field at all, so its own result is a successful,
    non-empty answer with no failure in it. Passing that on as emptiness would
    report a satellite the archive plainly has as one it does not — so the
    resolver files its own refusal, and leaves the batch's account of what the
    service said exactly as it found it.
    """
    carried = frame_of(
        KIND_TLE,
        [(A, OBS - 0.5)],
        **{CHECKSUM_STATUS_FIELD: CHECKSUM_UNVERIFIED_MISSING},
    )
    served = []
    real_batch = service.fetch_nearest_batch

    def spy(norad_ids, epoch_jd, **kwargs):
        served.append(real_batch(norad_ids, epoch_jd, **kwargs))
        return served[-1]

    monkeypatch.setattr(service, "fetch_nearest_batch", spy)

    resolution = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        **_quiet(
            endpoints=(StubEndpoint("first", answers={A: carried}).pair,),
            fallback=False,
            allow_missing_checksum=False,
        ),
    )

    error = resolution.service_errors[A]
    assert isinstance(error, SatCheckerResponseError)
    assert "first" in str(error)
    assert A not in resolution.unavailable and A not in resolution.resolved
    attempt = resolution.attempts[A][0]
    assert (attempt.status, attempt.error) == (ATTEMPT_ERROR, error)

    (batch,) = served
    assert batch.errors == {} and batch.outage is None
    assert batch.records["NORAD_CAT_ID"].tolist() == [A]


def test_events_and_callbacks_arrive_on_the_calling_thread(tmp_path):
    """A callback is the application's code, run where the application called.

    The fetches happen in a bounded pool; delivering events from those threads
    would make an application's own reporting concurrent without its ever
    asking for that, and an exception raised in one would never reach the
    caller.
    """
    caller = threading.get_ident()
    workers = set()
    delivered = []

    def answer(norad_id, epoch_jd):
        workers.add(threading.get_ident())
        time.sleep(0.01)
        return frame_of(KIND_TLE, [(norad_id, OBS - 0.5)])

    wanted = list(range(41000, 41010))
    endpoint = StubEndpoint("first", default=answer)

    resolution = resolve_orbits(
        wanted,
        OBS,
        log=lambda _m: None,
        on_event=lambda event: delivered.append(threading.get_ident()),
        **_quiet(
            endpoints=(endpoint.pair,),
            cache=TextOrbitCache(tmp_path / "cache"),
            max_workers=5,
        ),
    )

    assert resolution.complete
    assert workers and caller not in workers
    assert delivered and set(delivered) == {caller}


def _documented_report(resolution) -> list:
    """The outcome-handling loop from ``docs/usage.md``, printing into a list.

    Copied rather than imported: what is being checked is that the example a
    reader lifts off the page runs against every shape this result takes —
    including the rejection with nothing measurable in it, which an unbranched
    ``{age_days:.2f}`` turns into a ``TypeError`` in the caller's error path.
    """
    reported = []
    for norad_id in resolution.missing:
        if norad_id in resolution.service_errors:
            raise resolution.service_errors[norad_id]
        rejected = resolution.rejected.get(norad_id)
        if rejected is not None and rejected.reason_code == "over_age":
            reported.append(
                f"{norad_id}: nearest record was {rejected.age_days:.2f} d away, "
                f"over {rejected.limit_name}={rejected.ceiling_days}"
            )
        elif rejected is not None:
            reported.append(
                f"{norad_id}: the record found for it was unusable "
                f"({rejected.reason_code})"
            )
        if norad_id in resolution.unavailable:
            reported.append(f"{norad_id}: {resolution.unavailable[norad_id]}")
    return reported


def test_the_documented_outcome_report_runs_over_every_outcome(tmp_path):
    """One accepted record, one refused on age, one unusable, two with nothing."""
    cache = TextOrbitCache(tmp_path / "cache")
    cache.store(B, frame_of(KIND_TLE, [(B, OBS - 5.0)]))
    extra = pd.concat(
        [extra_frame([(KIND_TLE, A, OBS - 0.5)]), _corrupt_frame(C, OBS - 0.5)],
        ignore_index=True,
    )

    offline = resolve_orbits(
        [A, B, C, D],
        OBS,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_EXTRA, GROUP_REMOTE),
            offline=True,
            remote_max_age_days=3.0,
            extra_orbit_max_age_days=3.0,
            cache=cache,
            endpoints=(ForbiddenEndpoint("first").pair, ForbiddenEndpoint("second").pair),
        ),
        extra_records=extra,
    )

    assert offline.resolved[A].source == SOURCE_EXTRA
    assert offline.rejected[B].reason_code == REASON_OVER_AGE
    assert offline.rejected[C].reason_code == REASON_INVALID
    # The fact the example has to branch on: nothing about C's record could be
    # measured, so its rejection carries no age, no offset and no ceiling.
    assert (offline.rejected[C].age_days, offline.rejected[C].ceiling_days) == (
        None,
        None,
    )
    assert offline.unavailable[B] == UNAVAILABLE_OFFLINE
    assert offline.unavailable[C] == UNAVAILABLE_INVALID_LOCAL
    assert offline.unavailable[D] == UNAVAILABLE_OFFLINE

    assert _documented_report(offline) == [
        f"{B}: nearest record was 5.00 d away, over remote_max_age_days=3.0",
        f"{B}: offline",
        f"{C}: the record found for it was unusable (invalid)",
        f"{C}: invalid_local",
        f"{D}: offline",
    ]

    failed = resolve_orbits(
        [E],
        OBS,
        log=lambda _m: None,
        **_quiet(
            endpoints=(
                StubEndpoint(
                    "first", answers={E: SatCheckerResponseError("malformed")}
                ).pair,
            ),
            fallback=False,
        ),
    )
    assert E not in failed.unavailable
    with pytest.raises(SatCheckerResponseError):
        _documented_report(failed)


@pytest.mark.parametrize("strict", [True, False])
def test_strict_response_is_forwarded_to_every_endpoint(strict):
    """Every endpoint is told, explicitly; none is left on the library default."""
    first = StubEndpoint("first", require_strict=True)
    second = StubEndpoint(
        "second",
        answers={A: frame_of(KIND_TLE, [(A, OBS - 0.5)])},
        require_strict=True,
    )

    resolution = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        **_quiet(
            endpoints=(first.pair, second.pair), fallback=True, strict_response=strict
        ),
    )

    assert resolution.resolved[A].endpoint == "second"
    assert first.strict_flags == [strict]
    assert second.strict_flags == [strict]


@pytest.mark.parametrize(
    "payload", [{"error": "unavailable"}, {"version": "1.7.0"}, {"orbital_data": None}]
)
def test_http_200_error_is_failure_only_when_strict_selected(monkeypatch, payload):
    """The lenient parser is unchanged; strictness is the resolver's opt-in.

    Read leniently, every one of these replies is "this satellite has no
    record" — the one answer that must not be confused with a service that is
    failing, since it silently removes a requested satellite from a run.
    """
    monkeypatch.setattr(client, "_http_get", lambda *a, **k: json.dumps(payload).encode())

    strict = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        **_quiet(
            endpoints=nearest_endpoints_for(OBS), fallback=True, strict_response=True
        ),
    )
    assert isinstance(strict.service_errors[A], SatCheckerResponseError)
    assert A not in strict.unavailable

    lenient = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        **_quiet(
            endpoints=nearest_endpoints_for(OBS), fallback=True, strict_response=False
        ),
    )
    assert lenient.service_errors == {}
    assert lenient.unavailable[A] == UNAVAILABLE_ABSENT
    # And the parser itself still reads it as it always has.
    assert client.fetch_nearest_tle(A, OBS).empty


def test_fallback_success_moves_error_to_refresh_errors():
    """An ID the fallback resolved is no longer a coverage failure — but the
    request that failed is still a failed request, and has to reach the log."""
    failure = SatCheckerResponseError("malformed")
    first = StubEndpoint("first", answers={A: failure})
    second = StubEndpoint("second", answers={A: frame_of(KIND_TLE, [(A, OBS - 0.5)])})

    resolution = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        **_quiet(endpoints=(first.pair, second.pair), fallback=True),
    )

    assert resolution.resolved[A].endpoint == "second"
    assert resolution.service_errors == {}
    assert resolution.refresh_errors[A] is failure
    assert [attempt.status for attempt in resolution.attempts[A]] == [
        ATTEMPT_ERROR,
        ATTEMPT_USABLE,
    ]
    assert resolution.attempts[A][0].error is failure


@pytest.mark.parametrize(
    "failure",
    [
        SatCheckerTransportError("connection refused"),
        SatCheckerRateLimitError("slow down", retry_after=90.0),
        SatCheckerResponseError("malformed"),
        "invalid-record",
    ],
)
def test_cached_incumbent_survives_refresh_failure(tmp_path, failure):
    cache = TextOrbitCache(tmp_path / "cache")
    cache.store(A, frame_of(KIND_TLE, [(A, OBS - 2.0)]))
    answer = _corrupt_frame(A, OBS - 0.5) if failure == "invalid-record" else failure
    first = StubEndpoint("first", answers={A: answer})
    second = StubEndpoint("second", answers={A: answer})
    events = EventLog()

    resolution = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        on_event=events,
        **_quiet(
            cache=cache,
            cache_reuse_max_age_days=1.0,
            endpoints=(first.pair, second.pair),
            fallback=True,
        ),
    )

    assert resolution.resolved[A].source == SOURCE_CACHE
    assert resolution.service_errors == {}
    assert A in resolution.refresh_errors
    # Named with the source it is continuing from: saying "from the cache" for
    # an ID a second archive answered would describe a record the run never held.
    retained = events.of(EVENT_REFRESH_FAILED)
    assert retained and all(event.source == SOURCE_CACHE for event in retained)


# ---------------------------------------------------------------------------
# Outages
# ---------------------------------------------------------------------------

def test_outage_stops_fallback_and_marks_unsent_ids():
    """A service that cannot serve us is not asked a different question."""
    wanted = list(range(20000, 20030))
    first = StubEndpoint(
        "first", default=SatCheckerTransportError("connection refused")
    )
    second = StubEndpoint("second")

    resolution = resolve_orbits(
        wanted,
        OBS,
        log=lambda _m: None,
        **_quiet(endpoints=(first.pair, second.pair), fallback=True, max_workers=3),
    )

    assert len(first.requested) <= 3
    assert second.requested == []
    assert set(resolution.service_errors) == set(wanted)
    assert resolution.unavailable == {}


def test_outage_preventing_fallback_is_not_absence(monkeypatch):
    """An ID that returned empty is still unknown if its fallback never ran."""
    batches = []
    outage = SatCheckerTransportError("connection refused")

    def controlled(norad_ids, epoch_jd, **kwargs):
        batches.append(kwargs["endpoint"])
        if len(batches) > 1:  # pragma: no cover - the assertion is that we never get here
            raise AssertionError("a second endpoint was asked after an outage")
        return NearestBatchResult(
            records=pd.DataFrame(), errors={B: outage}, outage=outage
        )

    monkeypatch.setattr(service, "fetch_nearest_batch", controlled)
    first = StubEndpoint("first")
    second = StubEndpoint("second")

    resolution = resolve_orbits(
        [A, B],
        OBS,
        log=lambda _m: None,
        **_quiet(endpoints=(first.pair, second.pair), fallback=True),
    )

    assert batches == ["first"]
    assert resolution.service_errors[B] is outage
    # A answered "no record" — but the endpoint that would have been asked next
    # was never reached, so nothing here says the catalogue has none.
    assert A not in resolution.unavailable
    assert [attempt.status for attempt in resolution.attempts[A]] == [
        ATTEMPT_EMPTY,
        ATTEMPT_NOT_SENT,
    ]
    assert resolution.events and any(
        event.code == EVENT_OUTAGE for event in resolution.events
    )


def test_response_wall_stops_resolver_fallback():
    """A uniform wall of rejections is a blocked client, not absent satellites."""
    wanted = list(range(30000, 30000 + RESPONSE_WALL_THRESHOLD + 10))
    first = StubEndpoint("first", default=SatCheckerResponseError("blocked", status=403))
    second = StubEndpoint("second")

    resolution = resolve_orbits(
        wanted,
        OBS,
        log=lambda _m: None,
        **_quiet(endpoints=(first.pair, second.pair), fallback=True, max_workers=5),
    )

    assert len(first.requested) <= RESPONSE_WALL_THRESHOLD + 5
    assert second.requested == []
    assert set(resolution.service_errors) == set(wanted)
    assert UNAVAILABLE_ABSENT not in resolution.unavailable.values()


@pytest.mark.parametrize("workers", [1, 3, 5])
def test_max_workers_is_forwarded_and_bounds_calls(monkeypatch, workers):
    """The configured bound reaches the existing scheduler and actually holds."""
    real_batch = service.fetch_nearest_batch
    seen = []

    def spy(norad_ids, epoch_jd, **kwargs):
        seen.append(kwargs.get("max_workers"))
        return real_batch(norad_ids, epoch_jd, **kwargs)

    monkeypatch.setattr(service, "fetch_nearest_batch", spy)

    active = 0
    peak = 0
    lock = threading.Lock()

    def answer(norad_id, epoch_jd):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return frame_of(KIND_TLE, [(norad_id, OBS - 0.5)])

    wanted = list(range(40000, 40010))
    endpoint = StubEndpoint("first", default=answer)

    resolution = resolve_orbits(
        wanted,
        OBS,
        log=lambda _m: None,
        **_quiet(endpoints=(endpoint.pair,), max_workers=workers),
    )

    assert seen == [workers]
    assert peak <= workers
    assert resolution.complete


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

def _mixed_run(tmp_path, name, on_event=None, log=None):
    """One run exercising fallback, rejection, a retained incumbent and an outage.

    The outage is on the *last* endpoint deliberately: an outage on the first
    would stop the fallback this run also needs to exercise. Each run gets a
    cache of its own, since a run that stores a record changes the next one.
    """
    cache = TextOrbitCache(tmp_path / name)
    cache.store(D, frame_of(KIND_TLE, [(D, OBS - 2.0)]))
    first = StubEndpoint(
        "first",
        answers={
            A: None,
            B: frame_of(KIND_TLE, [(B, OBS - 20.0)]),
            D: SatCheckerResponseError("malformed"),
        },
    )
    second = StubEndpoint(
        "second",
        answers={
            A: frame_of(KIND_TLE, [(A, OBS - 0.5)]),
            B: frame_of(KIND_TLE, [(B, OBS - 20.0)]),
            D: SatCheckerTransportError("connection refused"),
        },
    )
    return resolve_orbits(
        [A, B, D],
        OBS,
        log=log or (lambda _m: None),
        on_event=on_event,
        **_quiet(
            cache=cache,
            cache_reuse_max_age_days=0.5,
            endpoints=(first.pair, second.pair),
            fallback=True,
        ),
    )


def test_events_preserve_facts_without_application_wording(tmp_path, monkeypatch):
    """The client reports facts; both applications own the sentences about them."""
    monkeypatch.setenv("TABSIM_TLE_LOG_DETAIL", "1")
    monkeypatch.setenv("TABASCAL_TLE_LOG_DETAIL", "1")
    events = EventLog()

    resolution = _mixed_run(tmp_path, "with-callback", on_event=events)

    assert resolution.resolved[A].endpoint == "second"
    assert resolution.rejected[B].reason_code == REASON_OVER_AGE
    assert resolution.resolved[D].source == SOURCE_CACHE
    # Facts a callback sees are the facts the result keeps: an application that
    # installs no callback is not reading a different run. Compared as a
    # multiset, since per-satellite events come from a concurrent batch.
    assert sorted(event.code for event in resolution.events) == sorted(events.codes)
    without_callback = _mixed_run(tmp_path, "without-callback")
    assert sorted(event.code for event in without_callback.events) == sorted(
        events.codes
    )

    for code in (EVENT_ENDPOINT_FALLBACK, EVENT_OUTAGE, EVENT_REFRESH_FAILED):
        assert code in events.codes, code
    assert events.for_id(B)
    for event in events.events:
        text = f"{event.code} {event.details!r} {event.source!r} {event.endpoint!r}"
        assert "TABSIM" not in text.upper()
        assert "TABASCAL" not in text.upper()


class _CallbackFailure(Exception):
    """A sentinel the resolver must not swallow or reclassify."""


def test_event_callback_failure_propagates(tmp_path):
    def explode(event):
        raise _CallbackFailure(event.code)

    with pytest.raises(_CallbackFailure):
        _mixed_run(tmp_path, "explode", on_event=explode)
