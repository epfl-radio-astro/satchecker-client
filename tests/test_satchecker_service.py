"""Offline tests for the batching and endpoint-selection layer.

Ported from TABASCAL's ``test_orbit_policy.py``: the tests here are the ones
that exercise :mod:`satchecker_client.service` directly, with no policy layer
in between. The policy tests (cache precedence, age ceilings, archive
failover) stayed with TABASCAL, where the policy lives.
"""

import threading
import time

import pandas as pd
import pytest

from satchecker_client import client, service
from satchecker_client._time import jd_to_datetime
from satchecker_client.client import (
    SatCheckerRateLimitError,
    SatCheckerResponseError,
    SatCheckerTransportError,
)
from satchecker_client.service import (
    NearestBatchResult,
    fetch_nearest_batch,
    nearest_endpoints_for,
    store_or_warn,
    validated_records,
)

from .tle_helpers import (  # noqa: F401  block_network is an autouse fixture
    block_network,
    jd,
    make_catalogue_df,
)


OBS = jd(2023, 2, 21)


# ---------------------------------------------------------------------------
# Bounded concurrent batching
# ---------------------------------------------------------------------------

def test_batch_uses_bounded_concurrency_and_preserves_input_order():
    active = 0
    maximum = 0
    lock = threading.Lock()

    def fetch(norad_id, epoch):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return make_catalogue_df([(norad_id, epoch)])

    result = fetch_nearest_batch([5, 4, 3, 2, 1], OBS, fetch_nearest=fetch, max_workers=2)
    assert maximum == 2
    assert result.records["NORAD_CAT_ID"].tolist() == [5, 4, 3, 2, 1]


def test_empty_input_makes_no_requests_and_returns_an_empty_result():
    result = fetch_nearest_batch(
        [], OBS, fetch_nearest=lambda *a: pytest.fail("no request expected")
    )
    assert isinstance(result, NearestBatchResult)
    assert result.records.empty and result.errors == {} and result.outage is None


def test_duplicate_ids_are_fetched_once():
    calls = []

    def fetch(norad_id, epoch):
        calls.append(norad_id)
        return make_catalogue_df([(norad_id, epoch)])

    result = fetch_nearest_batch([7, 7, 8, 7], OBS, fetch_nearest=fetch, max_workers=1)
    assert sorted(calls) == [7, 8]
    assert result.records["NORAD_CAT_ID"].tolist() == [7, 8]


def test_a_non_positive_worker_count_is_refused():
    with pytest.raises(ValueError, match="max_workers"):
        fetch_nearest_batch([1], OBS, fetch_nearest=lambda *a: None, max_workers=0)


def test_batch_collects_one_failure_without_losing_other_ids():
    def fetch(norad_id, epoch):
        if norad_id == 2:
            raise SatCheckerTransportError("offline")
        return make_catalogue_df([(norad_id, epoch)])

    result = fetch_nearest_batch([1, 2, 3], OBS, fetch_nearest=fetch)
    assert result.records["NORAD_CAT_ID"].tolist() == [1, 3]
    assert isinstance(result.errors[2], SatCheckerTransportError)


@pytest.mark.parametrize("workers", [2, 5, 16])
@pytest.mark.parametrize("latency", [0.0, 0.02])
def test_outage_costs_at_most_max_workers_requests(workers, latency):
    """An outage must cost exactly ``max_workers`` requests — no more, ever.

    The bound has to hold at *any* worker count and *any* failure latency. That
    is why requests are submitted incrementally: queueing all of them and
    cancelling the remainder leaks badly when failures return fast, because the
    pool's workers drain the queue faster than the cancel catches it.

    ``latency=0`` is the hard case (a refused connection); 20 ms stands in for a
    429 arriving in one round trip.
    """
    attempted = []
    lock = threading.Lock()

    def dead_service(norad_id, epoch):
        with lock:
            attempted.append(norad_id)
        if latency:
            time.sleep(latency)
        raise SatCheckerTransportError("connection refused")

    result = fetch_nearest_batch(
        list(range(1, 201)), OBS, fetch_nearest=dead_service,
        max_workers=workers, log=lambda _m: None,
    )
    assert len(attempted) <= workers
    # Every requested ID still gets an error, so coverage reporting names them all.
    assert set(result.errors) == set(range(1, 201))
    assert all(
        isinstance(error, SatCheckerTransportError) for error in result.errors.values()
    )
    assert result.outage is not None


def test_incremental_submission_still_fetches_every_id_when_healthy():
    """Topping the in-flight set up must not drop IDs off the end of the batch."""
    result = fetch_nearest_batch(
        list(range(1, 51)), OBS,
        fetch_nearest=lambda nid, epoch: make_catalogue_df([(nid, epoch)]),
        max_workers=4,
    )
    assert result.records["NORAD_CAT_ID"].tolist() == list(range(1, 51))
    assert result.errors == {}
    assert result.outage is None


def test_rate_limit_stops_the_batch_and_reports_the_wait():
    """A 429 is the service asking us to stop; the rest of the list must not go out."""
    attempted = []
    lock = threading.Lock()
    logged = []

    def limited(norad_id, epoch):
        with lock:
            attempted.append(norad_id)
        raise SatCheckerRateLimitError("slow down", retry_after=90.0)

    result = fetch_nearest_batch(
        list(range(1, 201)), OBS, fetch_nearest=limited, max_workers=3,
        log=logged.append,
    )
    assert len(attempted) <= 3
    assert set(result.errors) == set(range(1, 201))
    assert isinstance(result.outage, SatCheckerRateLimitError)
    assert any("rate-limiting" in line and "90 s" in line for line in logged)


def test_uniform_rejection_is_recognised_as_a_wall_not_missing_satellites():
    """A service answering 4xx to everything must not be asked once per satellite.

    A 4xx is normally per-request — an unknown catalogue ID legitimately 404s —
    so this can only be inferred from a run of identical statuses with no success
    in between, not from the first one.
    """
    attempted = []
    lock = threading.Lock()

    def walled(norad_id, epoch):
        with lock:
            attempted.append(norad_id)
        raise SatCheckerResponseError("blocked", status=403)

    result = fetch_nearest_batch(
        list(range(1, 201)), OBS, fetch_nearest=walled, max_workers=5,
        log=lambda _m: None,
    )
    assert len(attempted) <= service.RESPONSE_WALL_THRESHOLD + 5
    assert set(result.errors) == set(range(1, 201))
    assert result.outage is not None


def test_a_few_missing_satellites_do_not_trip_the_wall_detector():
    """Genuinely absent catalogue entries must not abort the healthy remainder."""
    missing = {3, 7, 11}

    def fetch(norad_id, epoch):
        if norad_id in missing:
            raise SatCheckerResponseError("no such object", status=404)
        return make_catalogue_df([(norad_id, epoch)])

    result = fetch_nearest_batch(
        list(range(1, 41)), OBS, fetch_nearest=fetch, max_workers=5,
        log=lambda _m: None,
    )
    assert set(result.errors) == missing
    assert result.records["NORAD_CAT_ID"].tolist() == [
        nid for nid in range(1, 41) if nid not in missing
    ]
    assert result.outage is None


def test_batch_response_errors_do_not_abandon_the_rest():
    """A malformed reply is one satellite's problem: the service is still up."""
    def fetch(norad_id, epoch):
        if norad_id % 2:
            raise SatCheckerResponseError("malformed")
        return make_catalogue_df([(norad_id, epoch)])

    result = fetch_nearest_batch(
        list(range(1, 11)), OBS, fetch_nearest=fetch, max_workers=2
    )
    assert result.records["NORAD_CAT_ID"].tolist() == [2, 4, 6, 8, 10]
    assert set(result.errors) == {1, 3, 5, 7, 9}


def test_a_record_for_the_wrong_satellite_is_an_error_not_a_result():
    """A reply whose record belongs to another ID must not be returned as ours."""
    def fetch(norad_id, epoch):
        return make_catalogue_df([(99999, epoch)])

    result = fetch_nearest_batch(
        [25544], OBS, fetch_nearest=fetch, log=lambda _m: None
    )
    assert result.records.empty
    assert isinstance(result.errors[25544], SatCheckerResponseError)
    assert "25544" in str(result.errors[25544])


def test_an_empty_reply_is_neither_a_record_nor_an_error():
    """Absence of a nearby record is for the caller's policy to judge, not us."""
    result = fetch_nearest_batch(
        [25544], OBS, fetch_nearest=lambda *a: pd.DataFrame()
    )
    assert result.records.empty and result.errors == {} and result.outage is None


# ---------------------------------------------------------------------------
# Record validation at the batch boundary
# ---------------------------------------------------------------------------

def test_validated_records_rejects_a_mismatched_embedded_id():
    frame = make_catalogue_df([(25544, OBS)])
    frame.loc[0, "NORAD_CAT_ID"] = 99999
    logged = []
    result = validated_records(frame, "test", log=logged.append)
    assert result.empty
    assert any("99999" in line for line in logged)


def test_validated_records_keeps_the_valid_rows_and_drops_the_broken_one():
    frame = make_catalogue_df([(1, OBS), (2, OBS), (3, OBS)])
    frame.loc[1, "TLE_LINE1"] = "garbage"
    result = validated_records(frame, "test", log=lambda _m: None)
    assert result["NORAD_CAT_ID"].tolist() == [1, 3]


def test_validated_records_passes_an_empty_frame_through():
    assert validated_records(pd.DataFrame(), "test", log=lambda _m: None).empty


# ---------------------------------------------------------------------------
# Resilient cache writes
# ---------------------------------------------------------------------------

def test_store_or_warn_reports_success_silently(tmp_path):
    logged = []
    assert store_or_warn(lambda: None, tmp_path, "records", log=logged.append)
    assert logged == []


def test_store_or_warn_survives_an_io_failure_and_says_so(tmp_path):
    def failing_write():
        raise OSError("disk full")

    logged = []
    assert not store_or_warn(failing_write, tmp_path, "records", log=logged.append)
    assert any("disk full" in line and str(tmp_path) in line for line in logged)


# ---------------------------------------------------------------------------
# Endpoint selection
# ---------------------------------------------------------------------------

BEFORE_HANDOVER = jd(2026, 6, 1)
AFTER_HANDOVER = jd(2026, 8, 1)


class TestEndpointSelection:
    """Which archive to ask, given when the observation was.

    The two archives do not overlap: TLEs stop at 2026-07-11 and OMM starts
    twelve hours later. So the observation epoch alone decides which endpoint is
    worth asking first, and asking the wrong one first costs a request rather
    than a wrong answer.
    """

    def _labels(self, epoch_jd):
        return [label for label, _ in nearest_endpoints_for(epoch_jd)]

    def test_a_pre_handover_epoch_asks_the_tle_archive_first(self):
        assert self._labels(BEFORE_HANDOVER)[0] == "nearest-TLE"

    def test_a_post_handover_epoch_asks_the_omm_archive_first(self):
        assert self._labels(AFTER_HANDOVER)[0] == "nearest-OMM"

    def test_the_boundary_itself_belongs_to_the_omm_archive(self):
        assert self._labels(client.HANDOVER_JD)[0] == "nearest-OMM"
        assert self._labels(client.HANDOVER_JD - 1e-6)[0] == "nearest-TLE"

    def test_both_endpoints_are_always_offered(self):
        # Neither endpoint reports "nothing that near", so the other is always
        # worth a try before concluding a satellite cannot be resolved.
        for epoch in (BEFORE_HANDOVER, AFTER_HANDOVER):
            assert len(self._labels(epoch)) == 2
            assert set(self._labels(epoch)) == {"nearest-TLE", "nearest-OMM"}

    def test_the_functions_are_resolved_at_call_time_not_import_time(self, monkeypatch):
        # The transport is patched through :mod:`satchecker_client.client`; a
        # binding taken at import time would quietly bypass the patch.
        sentinel = object()
        monkeypatch.setattr(client, "fetch_nearest_tle", sentinel)
        functions = dict(nearest_endpoints_for(BEFORE_HANDOVER))
        assert functions["nearest-TLE"] is sentinel

    def test_the_handover_matches_satcheckers_changelog(self):
        assert jd_to_datetime(client.HANDOVER_JD).date().isoformat() == "2026-07-12"
