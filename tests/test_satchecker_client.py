"""Offline contract tests for the two-endpoint SatChecker client."""

import email.utils
import json
import socket
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from satchecker_client import client
from satchecker_client.records import record_epoch_jd
from satchecker_client.client import (
    SatCheckerRateLimitError,
    SatCheckerResponseError,
    SatCheckerTransportError,
    fetch_nearest_omm,
    fetch_nearest_tle,
    search_satellites,
)

from .tle_helpers import (  # noqa: F401
    block_network,
    jd,
    make_nearest_json,
    make_nearest_omm_json,
)


EPOCH = jd(2023, 1, 1)


def test_nearest_response_is_normalised(monkeypatch):
    monkeypatch.setattr(client, "_http_get", lambda *args, **kwargs: make_nearest_json([(25544, EPOCH)]))
    frame = fetch_nearest_tle(25544, EPOCH)
    assert list(frame.columns) == client.TLE_COLUMNS + ["RECORD_KIND"]
    assert frame.loc[0, "NORAD_CAT_ID"] == 25544
    assert frame.loc[0, "RECORD_KIND"] == "tle"


@pytest.mark.parametrize("payload", [b"[]", b'[{"orbital_data": []}]'])
def test_no_record_is_an_empty_frame(monkeypatch, payload):
    monkeypatch.setattr(client, "_http_get", lambda *args, **kwargs: payload)
    assert fetch_nearest_tle(99999, EPOCH).empty


@pytest.mark.parametrize(
    "rows, message",
    [
        ([{"satellite_id": None, "tle_line1": "x", "tle_line2": "y"}], "missing satellite IDs"),
        ([{"satellite_id": 1.5, "tle_line1": "x", "tle_line2": "y"}], "non-integer"),
        ([{"satellite_id": 1, "tle_line1": None, "tle_line2": "y"}], "missing TLE_LINE1"),
    ],
)
def test_malformed_rows_raise_response_error(monkeypatch, rows, message):
    payload = json.dumps({"orbital_data": rows}).encode()
    monkeypatch.setattr(client, "_http_get", lambda *args, **kwargs: payload)
    with pytest.raises(SatCheckerResponseError, match=message):
        fetch_nearest_tle(1, EPOCH)


def test_invalid_json_is_a_response_error(monkeypatch):
    monkeypatch.setattr(client, "_http_get", lambda *args, **kwargs: b"not-json")
    with pytest.raises(SatCheckerResponseError, match="invalid JSON"):
        fetch_nearest_tle(25544, EPOCH)


@pytest.mark.parametrize("status", [400, 403, 404, 422])
def test_client_statuses_are_response_errors(monkeypatch, status):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("url", status, "reason", {}, None)

    monkeypatch.setattr(client.urllib.request, "urlopen", fail)
    with pytest.raises(SatCheckerResponseError, match=f"HTTP {status}"):
        fetch_nearest_tle(25544, EPOCH)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_backoff_and_server_statuses_are_transport_errors(monkeypatch, status):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("url", status, "reason", {}, None)

    monkeypatch.setattr(client.urllib.request, "urlopen", fail)
    with pytest.raises(SatCheckerTransportError, match=f"HTTP {status}"):
        fetch_nearest_tle(25544, EPOCH)


def _raise_429(monkeypatch, headers):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("url", 429, "Too Many Requests", headers, None)

    monkeypatch.setattr(client.urllib.request, "urlopen", fail)


def test_rate_limit_is_its_own_error_type(monkeypatch):
    """429 must be distinguishable: it is about this client, not this satellite."""
    _raise_429(monkeypatch, {})
    with pytest.raises(SatCheckerRateLimitError) as caught:
        fetch_nearest_tle(25544, EPOCH)
    assert caught.value.retry_after is None
    # Still a transport error, so a batch stops on it rather than working onward.
    assert isinstance(caught.value, SatCheckerTransportError)


def test_retry_after_delta_seconds_is_reported(monkeypatch):
    _raise_429(monkeypatch, {"Retry-After": "120"})
    with pytest.raises(SatCheckerRateLimitError, match="120 s before the next request"):
        fetch_nearest_tle(25544, EPOCH)


def test_retry_after_http_date_is_reported(monkeypatch):
    """RFC 9110 permits an HTTP-date as well as delta-seconds."""
    when = datetime.now(timezone.utc) + timedelta(seconds=90)
    _raise_429(monkeypatch, {"Retry-After": email.utils.format_datetime(when)})
    with pytest.raises(SatCheckerRateLimitError) as caught:
        fetch_nearest_tle(25544, EPOCH)
    assert caught.value.retry_after == pytest.approx(90, abs=5)


@pytest.mark.parametrize("value", ["", "not-a-date", "Mon, 99 Xxx 9999"])
def test_unusable_retry_after_does_not_become_a_second_failure(monkeypatch, value):
    """A bad hint must degrade to 'no hint', never to an exception of its own."""
    _raise_429(monkeypatch, {"Retry-After": value})
    with pytest.raises(SatCheckerRateLimitError) as caught:
        fetch_nearest_tle(25544, EPOCH)
    assert caught.value.retry_after is None


def test_elapsed_retry_after_clamps_to_zero(monkeypatch):
    when = datetime.now(timezone.utc) - timedelta(hours=1)
    _raise_429(monkeypatch, {"Retry-After": email.utils.format_datetime(when)})
    with pytest.raises(SatCheckerRateLimitError) as caught:
        fetch_nearest_tle(25544, EPOCH)
    assert caught.value.retry_after == 0.0


@pytest.mark.parametrize("error", [socket.timeout("slow"), OSError("dropped")])
def test_network_failures_are_transport_errors(monkeypatch, error):
    monkeypatch.setattr(client.urllib.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(error))
    with pytest.raises(SatCheckerTransportError, match="request failed"):
        fetch_nearest_tle(25544, EPOCH)


# ---------------------------------------------------------------------------
# get-nearest-omm
# ---------------------------------------------------------------------------

class TestFetchNearestOmm:
    """The second endpoint, and the ways its response differs from the first.

    The envelope is the same and the transport, retry and status classification
    are shared verbatim. What is new is a level of nesting, an epoch that
    appears twice in two different spellings, and the absence of any structural
    check as strong as a TLE checksum.
    """

    def _serve(self, monkeypatch, payload):
        monkeypatch.setattr(client, "_http_get", lambda *a, **k: payload)

    def test_response_is_normalised(self, monkeypatch):
        self._serve(monkeypatch, make_nearest_omm_json([(25544, EPOCH)]))
        frame = fetch_nearest_omm(25544, EPOCH)
        assert list(frame.columns) == client.OMM_COLUMNS + ["RECORD_KIND"]
        assert frame.loc[0, "NORAD_CAT_ID"] == 25544
        assert frame.loc[0, "RECORD_KIND"] == "omm"
        assert frame.loc[0, "OBJECT_ID"] == "1998-067A"

    def test_nested_elements_are_lifted_onto_flat_columns(self, monkeypatch):
        self._serve(monkeypatch, make_nearest_omm_json([(25544, EPOCH)]))
        frame = fetch_nearest_omm(25544, EPOCH)
        for column in (
            "INCLINATION",
            "RA_OF_ASC_NODE",
            "ECCENTRICITY",
            "ARG_OF_PERICENTER",
            "MEAN_ANOMALY",
            "MEAN_MOTION",
            "BSTAR",
        ):
            assert isinstance(frame.loc[0, column], float), column
        assert "orbital_elements" not in frame.columns

    def test_the_nested_iso_epoch_wins_over_the_row_level_one(self, monkeypatch):
        # The row carries "2023-01-01 00:00:00 UTC", which is neither ISO 8601
        # nor sub-second; the nested object carries the parseable spelling. Take
        # the wrong one and every OMM record fails to yield an epoch at all.
        self._serve(monkeypatch, make_nearest_omm_json([(25544, EPOCH)]))
        frame = fetch_nearest_omm(25544, EPOCH)
        assert "UTC" not in frame.loc[0, "EPOCH"]
        assert record_epoch_jd(frame.loc[0]) == pytest.approx(EPOCH, abs=1e-6)

    def test_unused_element_fields_are_dropped(self, monkeypatch):
        self._serve(monkeypatch, make_nearest_omm_json([(25544, EPOCH)]))
        frame = fetch_nearest_omm(25544, EPOCH)
        for dropped in ("REV_AT_EPOCH", "ELEMENT_SET_NO", "MEAN_MOTION_DOT"):
            assert dropped not in frame.columns

    @pytest.mark.parametrize("payload", [b"[]", b'[{"orbital_data": []}]'])
    def test_no_record_is_an_empty_frame(self, monkeypatch, payload):
        # Confirmed against the live service: an unknown catalogue number comes
        # back as HTTP 200 with an empty orbital_data, not as a 404.
        self._serve(monkeypatch, payload)
        assert fetch_nearest_omm(999998, EPOCH).empty

    def test_a_row_without_the_nested_object_is_a_response_error(self, monkeypatch):
        payload = json.dumps([{"orbital_data": [{"satellite_id": 25544}]}]).encode()
        self._serve(monkeypatch, payload)
        with pytest.raises(SatCheckerResponseError, match="no orbital_elements"):
            fetch_nearest_omm(25544, EPOCH)

    @pytest.mark.parametrize(
        "missing",
        ["EPOCH", "INCLINATION", "MEAN_MOTION", "BSTAR", "ECCENTRICITY"],
    )
    def test_a_missing_element_field_is_a_response_error(self, monkeypatch, missing):
        raw = json.loads(make_nearest_omm_json([(25544, EPOCH)]))
        del raw[0]["orbital_data"][0]["orbital_elements"][missing]
        self._serve(monkeypatch, json.dumps(raw).encode())
        with pytest.raises(SatCheckerResponseError, match=f"missing {missing}"):
            fetch_nearest_omm(25544, EPOCH)

    @pytest.mark.parametrize(
        "satellite_id, message",
        [
            (None, "missing satellite IDs"),
            (1.5, "non-integer"),
            ("abc", "non-numeric"),
        ],
    )
    def test_the_id_checks_are_shared_with_the_tle_path(
        self, monkeypatch, satellite_id, message
    ):
        raw = json.loads(make_nearest_omm_json([(25544, EPOCH)]))
        raw[0]["orbital_data"][0]["satellite_id"] = satellite_id
        self._serve(monkeypatch, json.dumps(raw).encode())
        with pytest.raises(SatCheckerResponseError, match=message):
            fetch_nearest_omm(25544, EPOCH)

    def test_a_single_row_object_is_accepted(self, monkeypatch):
        raw = json.loads(make_nearest_omm_json([(25544, EPOCH)]))
        raw[0]["orbital_data"] = raw[0]["orbital_data"][0]
        self._serve(monkeypatch, json.dumps(raw).encode())
        assert len(fetch_nearest_omm(25544, EPOCH)) == 1

    def test_transport_failures_are_not_endpoint_specific(self, monkeypatch):
        def boom(*a, **k):
            raise SatCheckerTransportError("down")

        monkeypatch.setattr(client, "_http_get", boom)
        with pytest.raises(SatCheckerTransportError):
            fetch_nearest_omm(25544, EPOCH)

    def test_the_request_names_the_omm_endpoint(self, monkeypatch):
        seen = {}

        def capture(url, *a, **k):
            seen["url"] = url
            return make_nearest_omm_json([(25544, EPOCH)])

        monkeypatch.setattr(client, "_http_get", capture)
        fetch_nearest_omm(25544, EPOCH)
        assert "get-nearest-omm" in seen["url"]
        assert f"epoch={repr(float(EPOCH))}" in seen["url"].replace("%20", " ")

    def test_a_clamped_pre_handover_record_is_returned_not_hidden(self, monkeypatch):
        # The service answers a pre-handover request with its earliest record
        # instead of reporting that it has none. The client's job is to hand
        # that back faithfully; rejecting it on age is the policy layer's.
        earliest = jd(2026, 7, 11, 19, 56)
        self._serve(monkeypatch, make_nearest_omm_json([(25544, earliest)]))
        frame = fetch_nearest_omm(25544, jd(2021, 11, 1))
        assert len(frame) == 1
        assert record_epoch_jd(frame.loc[0]) == pytest.approx(earliest, abs=1e-6)


class TestRequestedSatelliteFiltering:
    """A response must not be presented as a satellite it does not describe."""

    def test_a_response_for_a_different_satellite_is_an_error(self, monkeypatch):
        monkeypatch.setattr(
            client, "_http_get", lambda *a, **k: make_nearest_json([(99999, EPOCH)])
        )
        with pytest.raises(SatCheckerResponseError, match="99999"):
            fetch_nearest_tle(25544, EPOCH)

    def test_the_omm_endpoint_gets_the_same_check(self, monkeypatch):
        monkeypatch.setattr(
            client, "_http_get", lambda *a, **k: make_nearest_omm_json([(99999, EPOCH)])
        )
        with pytest.raises(SatCheckerResponseError, match="99999"):
            fetch_nearest_omm(25544, EPOCH)

    def test_stray_rows_for_other_satellites_are_dropped(self, monkeypatch):
        monkeypatch.setattr(
            client,
            "_http_get",
            lambda *a, **k: make_nearest_json([(25544, EPOCH), (99999, EPOCH)]),
        )
        frame = fetch_nearest_tle(25544, EPOCH)
        assert frame["NORAD_CAT_ID"].tolist() == [25544]

    def test_a_malformed_stray_tle_row_is_dropped_before_validation(self, monkeypatch):
        payload = json.loads(make_nearest_json([(25544, EPOCH), (99999, EPOCH)]))
        payload["orbital_data"][1]["tle_line2"] = None
        monkeypatch.setattr(
            client, "_http_get", lambda *a, **k: json.dumps(payload).encode()
        )
        frame = fetch_nearest_tle(25544, EPOCH)
        assert frame["NORAD_CAT_ID"].tolist() == [25544]

    def test_a_malformed_stray_omm_row_is_dropped_before_validation(self, monkeypatch):
        payload = json.loads(
            make_nearest_omm_json([(25544, EPOCH), (99999, EPOCH)])
        )
        payload[0]["orbital_data"][1]["orbital_elements"] = None
        monkeypatch.setattr(
            client, "_http_get", lambda *a, **k: json.dumps(payload).encode()
        )
        frame = fetch_nearest_omm(25544, EPOCH)
        assert frame["NORAD_CAT_ID"].tolist() == [25544]


def _serve(monkeypatch, payload):
    monkeypatch.setattr(client, "_http_get", lambda *a, **k: payload)


def _single_row(payload: bytes) -> bytes:
    """The same reply with ``orbital_data`` as one row object, not a list of one.

    Both endpoints accept that shape today, so strict parsing has to keep
    accepting it: it is a documented success, not a malformed value.
    """
    body = json.loads(payload)
    envelope = body[0] if isinstance(body, list) else body
    envelope["orbital_data"] = envelope["orbital_data"][0]
    return json.dumps(body).encode()


_TLE_ROWS = json.loads(make_nearest_json([(25544, EPOCH)]))["orbital_data"]

#: Both nearest endpoints, with the name that must appear in a strict error.
_BOTH_NEAREST = pytest.mark.parametrize(
    "fetch, endpoint",
    [(fetch_nearest_tle, "get-nearest-tle"), (fetch_nearest_omm, "get-nearest-omm")],
    ids=["nearest-tle", "nearest-omm"],
)


class TestStrictNearestResponses:
    """``strict_response=True``: absence and outage must stop looking alike.

    The default parser reads a nearest-record reply leniently — an error
    envelope served with HTTP 200, a missing data field and a null one all
    become "this satellite has no record", which is the same answer a genuinely
    empty archive gives. A caller that has to tell those apart, because a wrong
    answer means silently simulating without a satellite it asked for, opts in
    here. The documented empty forms stay empty; everything else raises.

    The default is unchanged, deliberately: tabascal calls these functions and
    does not opt in.
    """

    @_BOTH_NEAREST
    @pytest.mark.parametrize(
        "payload, message",
        [
            # An error envelope served with HTTP 200 is not "no record", with or
            # without an empty data list beside it. The service's own wording is
            # the only thing that says what went wrong, so it has to be carried.
            (json.dumps({"error": "unavailable"}).encode(), "unavailable"),
            (
                json.dumps({"error": "unavailable", "orbital_data": []}).encode(),
                "unavailable",
            ),
            # No recognised data field at all: the reply says nothing about the
            # satellite, so it cannot be read as the satellite having no record.
            (json.dumps({}).encode(), "data field|orbital_data"),
            (json.dumps({"version": "1.7.0"}).encode(), "data field|orbital_data"),
            (json.dumps({"data": _TLE_ROWS}).encode(), "data field|orbital_data"),
            # Present but malformed. Selection is by presence, not truthiness:
            # each of these is falsy and today becomes an empty result.
            (json.dumps({"orbital_data": None}).encode(), "orbital_data"),
            (json.dumps({"orbital_data": False}).encode(), "orbital_data"),
            (json.dumps({"orbital_data": ""}).encode(), "orbital_data"),
            (json.dumps({"orbital_data": 0}).encode(), "orbital_data"),
            # A wrapper holding more than one envelope: today the first wins and
            # the rest vanish without a word.
            (
                json.dumps([{"orbital_data": []}, {"orbital_data": []}]).encode(),
                "2 objects",
            ),
        ],
        ids=[
            "error envelope",
            "error beside empty data",
            "empty envelope",
            "envelope with no data field",
            "rows under an unrecognised key",
            "null orbital_data",
            "false orbital_data",
            "empty-string orbital_data",
            "zero orbital_data",
            "two-object wrapper",
        ],
    )
    def test_strict_nearest_rejects_error_or_missing_data_envelope(
        self, monkeypatch, fetch, endpoint, payload, message
    ):
        _serve(monkeypatch, payload)
        with pytest.raises(SatCheckerResponseError, match=message) as caught:
            fetch(25544, EPOCH, strict_response=True)
        # The context matters as much as the failure: a caller collecting these
        # per satellite needs to know which archive answered this way.
        assert endpoint in str(caught.value)

    def test_strict_nearest_rejects_contradictory_data_fields(self, monkeypatch):
        # Only the TLE endpoint, because ``tle_data`` is the legacy spelling of
        # *its* field; whether the OMM endpoint recognises it at all is not a
        # contract worth pinning here.
        _serve(
            monkeypatch,
            json.dumps({"orbital_data": [], "tle_data": _TLE_ROWS}).encode(),
        )
        with pytest.raises(SatCheckerResponseError, match="tle_data"):
            fetch_nearest_tle(25544, EPOCH, strict_response=True)

    @_BOTH_NEAREST
    @pytest.mark.parametrize(
        "payload",
        [b"[]", b'{"orbital_data": []}', b'[{"orbital_data": []}]'],
        ids=["empty list", "empty orbital_data", "wrapped empty orbital_data"],
    )
    def test_strict_nearest_preserves_documented_empty_results(
        self, monkeypatch, fetch, endpoint, payload
    ):
        """Genuine absence must survive the stricter reading unchanged.

        These are the shapes the service actually uses to say "no record for
        this satellite" — confirmed against the live service — and strict mode
        would be useless if it turned them into failures too.
        """
        _serve(monkeypatch, payload)
        frame = fetch(99999, EPOCH, strict_response=True)
        assert frame.empty

    def test_strict_nearest_preserves_the_legacy_tle_data_field(self, monkeypatch):
        _serve(monkeypatch, b'{"tle_data": []}')
        assert fetch_nearest_tle(99999, EPOCH, strict_response=True).empty

    @pytest.mark.parametrize(
        "fetch, payload, columns",
        [
            (fetch_nearest_tle, make_nearest_json([(25544, EPOCH)]), client.TLE_COLUMNS),
            (
                fetch_nearest_omm,
                make_nearest_omm_json([(25544, EPOCH)]),
                client.OMM_COLUMNS,
            ),
            (
                fetch_nearest_tle,
                _single_row(make_nearest_json([(25544, EPOCH)])),
                client.TLE_COLUMNS,
            ),
            (
                fetch_nearest_omm,
                _single_row(make_nearest_omm_json([(25544, EPOCH)])),
                client.OMM_COLUMNS,
            ),
        ],
        ids=["tle", "omm", "tle single row object", "omm single row object"],
    )
    def test_strict_nearest_accepts_valid_tle_and_omm_envelopes(
        self, monkeypatch, fetch, payload, columns
    ):
        _serve(monkeypatch, payload)
        strict = fetch(25544, EPOCH, strict_response=True)
        assert list(strict.columns) == columns + ["RECORD_KIND"]
        assert strict["NORAD_CAT_ID"].tolist() == [25544]
        # Strict parsing rejects more replies; it must not normalise a good one
        # differently, value for value.
        pd.testing.assert_frame_equal(strict, fetch(25544, EPOCH))

    @_BOTH_NEAREST
    def test_strict_nearest_filters_to_the_requested_satellite(
        self, monkeypatch, fetch, endpoint
    ):
        build = make_nearest_json if endpoint == "get-nearest-tle" else make_nearest_omm_json
        _serve(monkeypatch, build([(25544, EPOCH), (99999, EPOCH)]))
        frame = fetch(25544, EPOCH, strict_response=True)
        assert frame["NORAD_CAT_ID"].tolist() == [25544]

        # And a reply made up entirely of another satellite stays an error
        # rather than becoming an empty result.
        _serve(monkeypatch, build([(99999, EPOCH)]))
        with pytest.raises(SatCheckerResponseError, match="99999"):
            fetch(25544, EPOCH, strict_response=True)


class TestDefaultNearestContract:
    """What the two endpoints do when nobody opts in — tabascal's contract.

    Strict parsing is additive. Every reply below is read exactly as it was
    before the option existed, including the lenient readings strict mode is
    there to refuse; changing any of them would change what an unmodified
    consumer resolves, without that consumer asking for anything.
    """

    @pytest.mark.parametrize(
        "fetch, payload, expected_ids",
        [
            (fetch_nearest_tle, make_nearest_json([(25544, EPOCH)]), [25544]),
            (fetch_nearest_omm, make_nearest_omm_json([(25544, EPOCH)]), [25544]),
            (fetch_nearest_tle, b"[]", []),
            (fetch_nearest_omm, b"[]", []),
            (fetch_nearest_tle, b'{"orbital_data": []}', []),
            (fetch_nearest_omm, b'[{"orbital_data": []}]', []),
            # Lenient by design, and left that way: these become empty results.
            (fetch_nearest_tle, b'{"tle_data": []}', []),
            (fetch_nearest_tle, json.dumps({"error": "unavailable"}).encode(), []),
            (fetch_nearest_omm, json.dumps({"error": "unavailable"}).encode(), []),
            (fetch_nearest_tle, json.dumps({}).encode(), []),
            (fetch_nearest_tle, json.dumps({"orbital_data": None}).encode(), []),
        ],
        ids=[
            "tle record", "omm record", "tle empty list", "omm empty list",
            "tle empty orbital_data", "omm wrapped empty orbital_data",
            "legacy tle_data", "tle error envelope", "omm error envelope",
            "empty envelope", "null orbital_data",
        ],
    )
    def test_nearest_default_contract_is_unchanged(
        self, monkeypatch, fetch, payload, expected_ids
    ):
        _serve(monkeypatch, payload)
        frame = fetch(25544, EPOCH)
        if expected_ids:
            assert frame["NORAD_CAT_ID"].tolist() == expected_ids
        else:
            assert frame.empty


def _search_row(norad_id, name, **fields):
    """One ``search-satellites`` row as the service sends it; unset fields null."""
    row = {
        "satellite_id": norad_id,
        "satellite_name": name,
        "international_designator": None,
        "rcs_size": None,
        "launch_date": None,
        "decay_date": None,
        "object_type": None,
    }
    row.update(fields)
    return row


def _search_payload(rows, **overrides):
    body = {
        "count": len(rows),
        "data": rows,
        "source": "IAU CPS SatChecker",
        "version": "1.8.0",
    }
    body.update(overrides)
    return json.dumps(body).encode()


class TestSearchSatellites:
    def test_rows_are_normalised_and_kept_as_served(self, monkeypatch):
        # Shapes observed in the live catalogue: one satellite under two names
        # whose rows carry different fields, and one object under two NORAD IDs.
        rows = [
            _search_row(
                64236, "STARLINK-11691", international_designator="2025-119D",
                launch_date="2025-06-03", object_type="PAYLOAD", rcs_size="LARGE",
            ),
            _search_row(
                64236, "STARLINK-11691 [DTC]", international_designator="2025-119D",
            ),
            _search_row(
                47406, "STARLINK-2133", decay_date="2026-01-30",
            ),
        ]
        monkeypatch.setattr(client, "_http_get", lambda *a, **k: _search_payload(rows))

        frame = search_satellites("STARLINK")

        assert list(frame.columns) == client.SEARCH_COLUMNS
        assert frame["NORAD_CAT_ID"].tolist() == [64236, 64236, 47406]
        assert frame["NORAD_CAT_ID"].dtype.kind == "i"
        assert frame.loc[0, "LAUNCH_DATE"] == "2025-06-03"
        # A missing value is None under pandas 2 and NaN under pandas 3's string
        # dtype; either way it is null.
        assert pd.isna(frame.loc[1, "LAUNCH_DATE"])
        assert frame.loc[1, "OBJECT_ID"] == "2025-119D"
        assert frame.loc[2, "DECAY_DATE"] == "2026-01-30"

    def test_the_name_is_sent_as_given(self, monkeypatch):
        # Case-sensitive on the service, so the query must not be re-cased.
        seen = []

        def capture(url, *a, **k):
            seen.append(url)
            return _search_payload([_search_row(14781, "OSCAR 9 (UoSAT 1)")])

        monkeypatch.setattr(client, "_http_get", capture)
        search_satellites("OSCAR 9 (UoSAT 1)")

        parsed = urllib.parse.urlsplit(seen[0])
        assert parsed.path.endswith("/search-satellites/")
        assert urllib.parse.parse_qs(parsed.query) == {"name": ["OSCAR 9 (UoSAT 1)"]}

    def test_no_match_is_an_empty_frame_with_the_columns(self, monkeypatch):
        monkeypatch.setattr(client, "_http_get", lambda *a, **k: _search_payload([]))
        frame = search_satellites("ZZZNOSUCHSAT")
        assert frame.empty
        assert list(frame.columns) == client.SEARCH_COLUMNS

    @pytest.mark.parametrize("name", ["", "   ", "\t"])
    def test_an_empty_name_is_refused_before_any_request(self, monkeypatch, name):
        def forbidden(*a, **k):
            raise AssertionError("an empty name must not reach the service")

        monkeypatch.setattr(client, "_http_get", forbidden)
        with pytest.raises(ValueError, match="entire catalogue"):
            search_satellites(name)

    @pytest.mark.parametrize("name", [None, 25544, b"NAVSTAR"])
    def test_a_non_string_name_is_a_type_error(self, name):
        with pytest.raises(TypeError):
            search_satellites(name)

    @pytest.mark.parametrize(
        "payload, message",
        [
            # An error envelope served with HTTP 200 is not "no matches", even
            # when it carries an empty data list.
            (json.dumps({"error": "unavailable"}).encode(), "reports an error"),
            (json.dumps({"error": "unavailable", "data": [], "count": 0}).encode(), "reports an error"),
            (json.dumps({"count": 0}).encode(), "no data field"),
            (json.dumps({"data": []}).encode(), "no integer count"),
            (json.dumps({"count": None, "data": []}).encode(), "no integer count"),
            (json.dumps({"count": 0, "data": {}}).encode(), "unexpected search-satellites rows"),
            (json.dumps({"count": 1, "data": [1]}).encode(), "unexpected search-satellites rows"),
            (b"[]", "a list of 0 objects"),
            (b"[1]", "unexpected response shape"),
            (
                json.dumps([json.loads(_search_payload([])), json.loads(_search_payload([]))]).encode(),
                "a list of 2 objects",
            ),
            (_search_payload([_search_row(1, "A")], count=5), "reports 5 matches but carries 1"),
            (_search_payload([_search_row(1, "A")], count="1"), "no integer count"),
            # True == 1 in Python, so a boolean count must be refused explicitly.
            (_search_payload([_search_row(1, "A")], count=True), "no integer count"),
        ],
    )
    def test_malformed_responses_raise(self, monkeypatch, payload, message):
        monkeypatch.setattr(client, "_http_get", lambda *a, **k: payload)
        with pytest.raises(SatCheckerResponseError, match=message):
            search_satellites("A")

    @pytest.mark.parametrize(
        "row, message",
        [
            (_search_row(None, "A"), "missing satellite IDs"),
            (_search_row(1.5, "A"), "non-integer"),
            (_search_row(1, None), "missing satellite names"),
            (_search_row(1, "A", launch_date="2024/10/20"), "unreadable LAUNCH_DATE"),
            # Dates are compared as strings downstream, so padding matters.
            (_search_row(1, "A", launch_date="2024-10-1"), "unreadable LAUNCH_DATE"),
            (_search_row(1, "A", decay_date="2024-1-20"), "unreadable DECAY_DATE"),
            (_search_row(1, "A", decay_date=20261020), "unreadable DECAY_DATE"),
        ],
    )
    def test_malformed_rows_raise(self, monkeypatch, row, message):
        monkeypatch.setattr(client, "_http_get", lambda *a, **k: _search_payload([row]))
        with pytest.raises(SatCheckerResponseError, match=message):
            search_satellites("A")

    def test_a_single_envelope_wrapped_in_a_list_is_accepted(self, monkeypatch):
        payload = json.dumps([json.loads(_search_payload([_search_row(1, "A")]))]).encode()
        monkeypatch.setattr(client, "_http_get", lambda *a, **k: payload)
        assert search_satellites("A")["NORAD_CAT_ID"].tolist() == [1]

    def test_missing_optional_fields_are_filled_not_fatal(self, monkeypatch):
        rows = [{"satellite_id": 25544, "satellite_name": "ISS (ZARYA)"}]
        monkeypatch.setattr(client, "_http_get", lambda *a, **k: _search_payload(rows))
        frame = search_satellites("ISS")
        assert list(frame.columns) == client.SEARCH_COLUMNS
        assert pd.isna(frame.loc[0, "DECAY_DATE"])

