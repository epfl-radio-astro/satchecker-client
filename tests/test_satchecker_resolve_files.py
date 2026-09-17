"""Offline tests for explicit orbit files and for checksum provenance.

An explicitly named directory is the one source where "cannot be read" must
never be indistinguishable from "has no record for this satellite": the second
falls through to the cache and the service, so the run is built from exactly the
records the user said not to use, and nothing in the log says the file they
pointed at was never read.

The other half of this module is the checksum policy, which has to be *one*
policy. A default that rejects an unverifiable line from the service and accepts
it from a file advertises strictness whose workaround is to save the record
once; and a provenance that can be upgraded lets one permissive run launder a
record into every strict run after it.
"""

import json
from pathlib import Path

import pandas as pd
import pytest

from satchecker_client import cache as cache_module
from satchecker_client.cache import TextOrbitCache
from satchecker_client.client import SatCheckerError, SatCheckerResponseError
from satchecker_client.records import (
    CHECKSUM_STATUS_FIELD,
    CHECKSUM_UNVERIFIED_MISSING,
    CHECKSUM_VERIFIED,
    KIND_TLE,
)
from satchecker_client.tle_parse import tle_epoch_jd

from .tle_helpers import (  # noqa: F401  block_network is an autouse fixture
    NO_CHECKSUM_PAIR,
    STRAY_BACKSLASH_PAIR,
    block_network,
    jd,
    make_catalogue_df,
    make_omm,
    make_tle,
    make_tle_record,
    with_checksum,
    write_legacy_omm_file,
    write_legacy_tle_file,
)
from .resolve_helpers import (
    ATTEMPT_ERROR,
    GROUP_EXTRA,
    GROUP_REMOTE,
    SOURCE_CACHE,
    ForbiddenEndpoint,
    StubEndpoint,
    forbid_acquisition,
    frame_of,
    orbit_input_error,
    policy,
    read_extra_orbit_dir,
    resolve_orbits,
    resolve_module,
    save_orbits_for_reuse,
)


OBS = jd(2023, 2, 21)

A, B, C = 25544, 38833, 43013


def _write_records(path, records) -> None:
    """A list-of-records orbit table, written with the standard-library encoder."""
    path.write_text(json.dumps(list(records)))


# ---------------------------------------------------------------------------
# Reading a directory the user named
# ---------------------------------------------------------------------------

def test_extra_directory_reads_sorted_tables_and_empty_files(tmp_path):
    write_legacy_tle_file(tmp_path / "a-tle.json", [(A, OBS - 1.0)])
    write_legacy_omm_file(tmp_path / "b-omm.json", [(B, OBS - 1.0)])
    _write_records(tmp_path / "c-records.json", [make_tle_record(C, OBS - 1.0)])
    (tmp_path / "d-empty.json").write_text("[]")
    (tmp_path / "notes.txt").write_text("not an orbit table, and not read")

    frame = read_extra_orbit_dir(tmp_path)

    # Sorted, so a directory of assorted exports reads the same way twice.
    assert frame["NORAD_CAT_ID"].tolist() == [A, B, C]
    assert frame["DATA_SOURCE"].tolist() == ["test", "test", "test"]
    assert frame.loc[0, "TLE_LINE1"] == make_tle(A, OBS - 1.0)[0]


def test_extra_directory_missing_or_not_a_directory_is_empty(tmp_path):
    """The path warning is the application's; the reader simply has nothing."""
    assert read_extra_orbit_dir(tmp_path / "absent").empty
    plain = tmp_path / "a-file.json"
    plain.write_text("[]")
    assert read_extra_orbit_dir(plain).empty


def test_extra_directory_reads_through_the_package_reader(tmp_path, monkeypatch):
    """The strict single-file reader is the seam, not a second parser."""
    seen = []
    real = cache_module.read_orbit_file

    def spy(path):
        seen.append(str(path))
        return real(path)

    monkeypatch.setattr(cache_module, "read_orbit_file", spy)
    write_legacy_tle_file(tmp_path / "a-tle.json", [(A, OBS - 1.0)])

    read_extra_orbit_dir(tmp_path)

    assert seen == [str(tmp_path / "a-tle.json")]


def _managed_envelope(tmp_path):
    cache = TextOrbitCache(tmp_path / "managed")
    cache.store(A, make_catalogue_df([(A, OBS)]))
    return cache.path(A).read_text()


@pytest.mark.parametrize(
    "prepare,row",
    [
        (lambda path, tmp: path.write_text("{not json"), None),
        (lambda path, tmp: path.mkdir(), None),
        (lambda path, tmp: path.write_text('[{"foo": 1}]'), None),
        (lambda path, tmp: path.write_text(_managed_envelope(tmp)), None),
        (
            lambda path, tmp: _write_records(
                path,
                [
                    make_tle_record(A, OBS - 1.0),
                    dict(make_omm(B, OBS - 1.0), NORAD_CAT_ID="25544.5"),
                ],
            ),
            1,
        ),
    ],
    ids=[
        "malformed JSON",
        "unreadable path",
        "not an orbit table",
        "a managed cache envelope",
        "a malformed identity on an unrequested row",
    ],
)
def test_extra_directory_errors_identify_file_and_row(tmp_path, prepare, row):
    """Every failure names the file, and a row failure names the row too."""
    directory = tmp_path / "extra"
    directory.mkdir()
    path = directory / "supplied.json"
    prepare(path, tmp_path)

    with pytest.raises(orbit_input_error()) as caught:
        read_extra_orbit_dir(directory)

    error = caught.value
    assert isinstance(error, SatCheckerError)
    assert str(error.path) == str(path)
    assert str(path) in str(error)
    assert error.row == row
    if row is None:
        assert error.__cause__ is not None


@pytest.mark.parametrize(
    "norad_id", ["25544.5", 25544.5, True, None, "", "not-an-id"]
)
def test_extra_directory_validates_identity_without_lossy_casts(tmp_path, norad_id):
    """A malformed identity stops the read; it never rounds into another satellite.

    ``to_numeric`` turns one into a null and the row then vanishes from a
    wanted-ID filter, so the service answers for the satellite the file was
    meant to supply. ``int()`` is worse: 25544.5 truncates to a different
    catalogue number.
    """
    _write_records(
        tmp_path / "supplied.json", [dict(make_omm(A, OBS - 1.0), NORAD_CAT_ID=norad_id)]
    )
    with pytest.raises(orbit_input_error()):
        read_extra_orbit_dir(tmp_path)


def test_extra_directory_keeps_a_large_identity_exact(tmp_path):
    """An ID above 2**53 must not go through a float on its way in."""
    huge = 9007199254740993
    _write_records(
        tmp_path / "supplied.json", [dict(make_omm(A, OBS - 1.0), NORAD_CAT_ID=huge)]
    )
    frame = read_extra_orbit_dir(tmp_path)
    assert frame["NORAD_CAT_ID"].tolist() == [huge]


def test_extra_reader_does_not_apply_checksum_policy(tmp_path):
    """Reading is not accepting: the lines come back as written, policy comes later."""
    epoch = tle_epoch_jd(NO_CHECKSUM_PAIR[0])
    _write_records(
        tmp_path / "supplied.json",
        [
            make_tle_record(
                A,
                epoch,
                TLE_LINE1=NO_CHECKSUM_PAIR[0],
                TLE_LINE2=NO_CHECKSUM_PAIR[1],
            )
        ],
    )

    frame = read_extra_orbit_dir(tmp_path)
    assert frame.loc[0, "TLE_LINE1"] == NO_CHECKSUM_PAIR[0]
    assert frame.loc[0, "TLE_LINE2"] == NO_CHECKSUM_PAIR[1]
    assert CHECKSUM_STATUS_FIELD not in frame.columns

    strict = resolve_orbits(
        [A],
        epoch,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_EXTRA,),
            extra_orbit_max_age_days=None,
            allow_missing_checksum=False,
            endpoints=(ForbiddenEndpoint().pair,),
        ),
        extra_records=frame,
    )
    assert A not in strict.resolved

    permissive = resolve_orbits(
        [A],
        epoch,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_EXTRA,),
            extra_orbit_max_age_days=None,
            allow_missing_checksum=True,
            endpoints=(ForbiddenEndpoint().pair,),
        ),
        extra_records=frame,
    )
    assert (
        permissive.resolved[A].record[CHECKSUM_STATUS_FIELD]
        == CHECKSUM_UNVERIFIED_MISSING
    )


# ---------------------------------------------------------------------------
# One checksum policy, on every route a record can arrive by
# ---------------------------------------------------------------------------

def _defective_frame(defect, norad_id, epoch_jd) -> pd.DataFrame:
    line1, line2 = make_tle(norad_id, epoch_jd)
    overrides = {}
    if defect in ("missing_line1", "missing_both"):
        line1 = line1[:68]
    if defect in ("missing_line2", "missing_both"):
        line2 = line2[:68]
    if defect == "corrupt":
        line1 = line1[:68] + str((int(line1[68]) + 1) % 10)
    if defect == "carried_unverified":
        overrides[CHECKSUM_STATUS_FIELD] = CHECKSUM_UNVERIFIED_MISSING
    if defect == "unknown_status":
        overrides[CHECKSUM_STATUS_FIELD] = "probably-fine"
    record = make_tle_record(
        norad_id, epoch_jd, TLE_LINE1=line1, TLE_LINE2=line2, **overrides
    )
    return pd.DataFrame([record])


@pytest.mark.parametrize("route", ["extra", "cache", "service"])
@pytest.mark.parametrize("allow", [False, True])
@pytest.mark.parametrize(
    "defect,accepted_when_allowed",
    [
        ("missing_line1", True),
        ("missing_line2", True),
        ("missing_both", True),
        ("carried_unverified", True),
        ("corrupt", False),
        ("unknown_status", False),
    ],
)
def test_checksum_policy_agrees_across_all_candidate_routes(
    tmp_path, monkeypatch, route, allow, defect, accepted_when_allowed
):
    """One policy, applied identically to a file, the cache and the service."""
    frame = _defective_frame(defect, A, OBS - 0.5)
    settings = dict(
        allow_missing_checksum=allow,
        remote_max_age_days=None,
        cache_reuse_max_age_days=None,
        extra_orbit_max_age_days=None,
    )
    extra_records = None

    if route == "extra":
        settings.update(
            source_order=(GROUP_EXTRA,), endpoints=(ForbiddenEndpoint().pair,)
        )
        extra_records = frame
    elif route == "cache":
        cache = TextOrbitCache(tmp_path / "cache")
        monkeypatch.setattr(cache, "get", lambda norad_id, log=None: frame.copy())
        monkeypatch.setattr(cache, "store", lambda norad_id, records: None)
        settings.update(
            source_order=(GROUP_REMOTE,),
            cache=cache,
            endpoints=(StubEndpoint("service").pair,),
        )
    else:
        settings.update(
            source_order=(GROUP_REMOTE,),
            cache=None,
            endpoints=(StubEndpoint("service", answers={A: frame}).pair,),
        )

    resolution = resolve_orbits(
        [A], OBS, log=lambda _m: None, **policy(**settings), extra_records=extra_records
    )

    expected = allow and accepted_when_allowed
    assert (A in resolution.resolved) is expected, (route, defect, allow)
    if expected:
        assert (
            resolution.resolved[A].record[CHECKSUM_STATUS_FIELD]
            == CHECKSUM_UNVERIFIED_MISSING
        )


def test_canonical_lines_reach_records_frame_and_save(tmp_path):
    """A repaired line is repaired *on the record*, not merely tolerated."""
    epoch = tle_epoch_jd(STRAY_BACKSLASH_PAIR[0])
    record = make_tle_record(
        26867,
        epoch,
        TLE_LINE1=STRAY_BACKSLASH_PAIR[0],
        TLE_LINE2=STRAY_BACKSLASH_PAIR[1],
    )
    extra = pd.DataFrame([record])

    resolution = resolve_orbits(
        [26867],
        epoch,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_EXTRA,),
            extra_orbit_max_age_days=None,
            endpoints=(ForbiddenEndpoint().pair,),
        ),
        extra_records=extra,
    )

    canonical = STRAY_BACKSLASH_PAIR[0][:-1]
    accepted = resolution.records()[0]
    assert accepted["TLE_LINE1"] == canonical
    assert accepted[CHECKSUM_STATUS_FIELD] == CHECKSUM_VERIFIED
    assert resolution.frame().loc[0, "TLE_LINE1"] == canonical

    path = save_orbits_for_reuse(
        tmp_path / "used_orbits.json", resolution.norad_ids(), resolution.records()
    )
    saved = json.loads(Path(path).read_text())
    assert list(saved["TLE_LINE1"].values()) == [canonical]

    # The caller's own frame is not rewritten on its behalf.
    assert extra.loc[0, "TLE_LINE1"] == STRAY_BACKSLASH_PAIR[0]


def test_unverified_records_never_reach_cache_store(tmp_path):
    """Unverifiable records serve this run and stay out of the shared cache.

    Every application reading these files does so at whatever version it is on,
    and 0.1.x rejects a whole file over one line it cannot validate. Carried
    provenance is the case the existing line-defect filter cannot see: the lines
    checksum, and the record is still one nothing has verified.
    """
    cache = TextOrbitCache(tmp_path / "cache")
    cache.store(A, frame_of(KIND_TLE, [(A, OBS - 3.0)]))
    cache.store(B, frame_of(KIND_TLE, [(B, OBS - 3.0)]))

    missing = _defective_frame("missing_both", A, OBS - 0.5)
    carried = _defective_frame("carried_unverified", B, OBS - 0.5)
    endpoint = StubEndpoint("service", answers={A: missing, B: carried})

    stored = []
    real_store = cache.store

    def spy(norad_id, records):
        stored.append(records.copy())
        return real_store(norad_id, records)

    cache.store = spy  # an instance attribute, so the real method stays reachable

    resolution = resolve_orbits(
        [A, B],
        OBS,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_REMOTE,),
            allow_missing_checksum=True,
            cache=cache,
            endpoints=(endpoint.pair,),
        ),
    )

    for norad_id in (A, B):
        assert (
            resolution.resolved[norad_id].record[CHECKSUM_STATUS_FIELD]
            == CHECKSUM_UNVERIFIED_MISSING
        )
    for frame in stored:
        if CHECKSUM_STATUS_FIELD in frame.columns:
            assert (frame[CHECKSUM_STATUS_FIELD] != CHECKSUM_UNVERIFIED_MISSING).all()
    # The verified history that was already there is exactly what is still there.
    assert len(cache.get(A, log=lambda _m: None)) == 1
    assert len(cache.get(B, log=lambda _m: None)) == 1


def test_invalid_service_provenance_cannot_replace_verified_cache_history(tmp_path):
    """A row this layer refuses is not a row the shared cache should learn.

    The cache's own validator predates the provenance field and cannot see an
    unknown status, and its deduplication keeps the incoming row over the
    perfectly good incumbent it matches. The run itself survives — it still
    holds its cached record — but the *next* one reads a record it has to
    reject, and offline there is nothing to replace it with.
    """
    cache = TextOrbitCache(tmp_path / "cache")
    cache.store(A, frame_of(KIND_TLE, [(A, OBS - 2.0)]))
    before = cache.path(A).read_text()

    # The same lines and the same provider as the cached record, with a status
    # made by something this package does not know.
    endpoint = StubEndpoint(
        "service", answers={A: _defective_frame("unknown_status", A, OBS - 2.0)}
    )

    resolution = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_REMOTE,),
            remote_max_age_days=3.0,
            cache_reuse_max_age_days=0.0,
            cache=cache,
            endpoints=(endpoint.pair,),
        ),
    )

    assert resolution.resolved[A].source == SOURCE_CACHE
    assert isinstance(resolution.refresh_errors[A], SatCheckerResponseError)
    assert resolution.attempts[A][0].status == ATTEMPT_ERROR
    assert cache.path(A).read_text() == before

    offline = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_REMOTE,),
            remote_max_age_days=3.0,
            offline=True,
            cache=cache,
            endpoints=(ForbiddenEndpoint().pair,),
        ),
    )
    assert offline.resolved[A].source == SOURCE_CACHE


def test_strict_policy_rejects_carried_unverified_after_rechecksum(tmp_path):
    """Recomputing a checksum digit does not verify the source that omitted it.

    This is the epoch-shift hazard in miniature: a record whose lines are
    rewritten and re-checksummed still carries the provenance of the lines it
    was derived from, and a strict run has to refuse it.
    """
    epoch = tle_epoch_jd(NO_CHECKSUM_PAIR[0])
    line1, line2 = (with_checksum(line) for line in NO_CHECKSUM_PAIR)
    assert len(line1) == 69 and len(line2) == 69
    record = make_tle_record(
        A,
        epoch,
        TLE_LINE1=line1,
        TLE_LINE2=line2,
        **{CHECKSUM_STATUS_FIELD: CHECKSUM_UNVERIFIED_MISSING},
    )
    extra = pd.DataFrame([record])

    def run(allow):
        return resolve_orbits(
            [A],
            epoch,
            log=lambda _m: None,
            **policy(
                source_order=(GROUP_EXTRA,),
                extra_orbit_max_age_days=None,
                allow_missing_checksum=allow,
                endpoints=(ForbiddenEndpoint().pair,),
            ),
            extra_records=extra,
        )

    assert A not in run(False).resolved
    accepted = run(True).resolved[A].record
    assert accepted[CHECKSUM_STATUS_FIELD] == CHECKSUM_UNVERIFIED_MISSING
    assert accepted["TLE_LINE1"] == line1


def test_omm_gets_no_checksum_claim(monkeypatch):
    """OMM has no checksum, so there is no claim to make about one."""
    forbid_acquisition(monkeypatch)
    sentinels = {"ECCENTRICITY": 0.0066635, "BSTAR": 3.2e-05}
    record = make_omm(
        A,
        OBS - 0.5,
        DATA_SOURCE="spacetrack",
        FETCHED_AT="2026-01-01T00:00:00Z",
        **sentinels,
    )

    resolution = resolve_orbits(
        [A],
        OBS,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_EXTRA,),
            extra_orbit_max_age_days=None,
            endpoints=(ForbiddenEndpoint().pair,),
        ),
        extra_records=pd.DataFrame([record]),
    )

    accepted = resolution.resolved[A].record
    assert CHECKSUM_STATUS_FIELD not in accepted
    assert accepted["DATA_SOURCE"] == "spacetrack"
    assert accepted["FETCHED_AT"] == "2026-01-01T00:00:00Z"
    for column, value in sentinels.items():
        assert accepted[column] == value, column


def test_orbit_input_error_is_a_satchecker_error():
    error_type = orbit_input_error()
    assert issubclass(error_type, SatCheckerError)
    assert error_type is resolve_module().OrbitInputError
