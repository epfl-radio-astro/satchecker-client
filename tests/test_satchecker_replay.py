"""Offline tests for freezing a run's orbital inputs and reading them back.

A replay exists to make one thing reproducible: the exact records a previous run
propagated. So every shortcut that would normally be reasonable is a defect
here — rounding a float to ten decimal places, skipping a row that will not
read, taking the first of two records for one satellite, asking the cache for
the one that is missing. Each of those silently changes the orbital input of a
run whose whole purpose is to keep it fixed.

The two files are read as a pair and as nothing else: no directory scan, no age
selection, no cache, no request.
"""

import glob as glob_module
import json
import math
import struct
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from satchecker_client import client
from satchecker_client.cache import (
    TextOrbitCache,
    read_legacy_tle_records,
    read_orbit_file,
)
from satchecker_client.records import (
    CHECKSUM_STATUS_FIELD,
    CHECKSUM_UNVERIFIED_MISSING,
    CHECKSUM_VERIFIED,
    OMM_ELEMENT_COLUMNS,
    record_elements,
)

from .tle_helpers import (  # noqa: F401  block_network is an autouse fixture
    NO_CHECKSUM_PAIR,
    block_network,
    jd,
    make_omm,
    make_tle,
    make_tle_record,
    with_checksum,
)
from .resolve_helpers import (
    GROUP_EXTRA,
    INPUT_CHECKSUM_POLICY,
    INPUT_IDENTITY,
    INPUT_INVALID_RECORD,
    INPUT_STRUCTURE,
    INPUT_UNREADABLE,
    ForbiddenEndpoint,
    forbid,
    forbid_acquisition,
    forbid_cache,
    load_replay_orbits,
    orbit_input_error,
    policy,
    read_extra_orbit_dir,
    replay_file_names,
    resolve_module,
    resolve_orbits,
    save_orbits_for_reuse,
    save_replay_orbits,
)


A, B, C = 25544, 38833, 43013

TLE_EPOCH = jd(2010, 5, 31)
OMM_EPOCH = jd(2026, 8, 13, 3, 34, 14)


def _directory(tmp_path, name="input_data") -> Path:
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _bits(value) -> bytes:
    """A double's exact bit pattern — the only comparison a replay may make."""
    return struct.pack("<d", float(value))


def _columns(rows) -> dict:
    """Rows in the column-oriented shape ``DataFrame.to_json()`` writes."""
    columns: list[str] = []
    for row in rows:
        columns += [column for column in row if column not in columns]
    return {
        column: {str(index): row.get(column) for index, row in enumerate(rows)}
        for column in columns
    }


def _write_pair(directory, ids_text, records_text) -> tuple[Path, Path]:
    ids_file, records_file = replay_file_names()
    (directory / ids_file).write_text(ids_text)
    (directory / records_file).write_text(records_text)
    return directory / ids_file, directory / records_file


def _replay_row(norad_id, record) -> dict:
    """A saved row as the writer projects one, for hand-built fixtures."""
    row = {"NORAD_CAT_ID": int(norad_id), "RECORD_KIND": record["RECORD_KIND"]}
    for column in (
        "OBJECT_NAME",
        "OBJECT_ID",
        "TLE_LINE1",
        "TLE_LINE2",
        CHECKSUM_STATUS_FIELD,
        "EPOCH",
        *OMM_ELEMENT_COLUMNS,
        "DATA_SOURCE",
        "FETCHED_AT",
    ):
        if column in record and record[column] is not None:
            row[column] = record[column]
    return row


# ---------------------------------------------------------------------------
# Exact round trips
# ---------------------------------------------------------------------------

def test_save_load_tle_omm_and_mixed_exactly(tmp_path):
    """What comes back is what went in, compared as strings and bit patterns.

    ``DataFrame.to_json`` writes 0.0066635 as 0.006663499999999999 at its
    maximum precision, which is a *different* double: a replayed trajectory
    then quietly stops matching the run the file was written to reproduce.
    """
    tle = make_tle_record(
        26867, TLE_EPOCH, DATA_SOURCE="spacetrack", FETCHED_AT="2026-01-01T00:00:00Z"
    )
    sentinels = make_omm(
        A, OMM_EPOCH, ECCENTRICITY=0.0066635, BSTAR=3.2e-05, DATA_SOURCE="celestrak"
    )
    signed_zero = make_omm(B, OMM_EPOCH, BSTAR=-0.0)
    subnormal = make_omm(C, OMM_EPOCH, BSTAR=5e-324)

    ids = [26867, A, B, C]
    records = [tle, sentinels, signed_zero, subnormal]
    directory = _directory(tmp_path)
    saved = save_replay_orbits(directory, ids, records)
    assert [Path(path).name for path in saved] == list(replay_file_names())

    loaded_ids, loaded = load_replay_orbits(directory, allow_missing_checksum=False)

    assert loaded_ids == ids
    assert [int(record["NORAD_CAT_ID"]) for record in loaded] == ids
    assert loaded[0]["TLE_LINE1"] == tle["TLE_LINE1"]
    assert loaded[0]["TLE_LINE2"] == tle["TLE_LINE2"]
    assert loaded[0][CHECKSUM_STATUS_FIELD] == CHECKSUM_VERIFIED
    assert loaded[0]["DATA_SOURCE"] == "spacetrack"
    assert loaded[0]["FETCHED_AT"] == "2026-01-01T00:00:00Z"

    for position, original in ((1, sentinels), (2, signed_zero), (3, subnormal)):
        for column in OMM_ELEMENT_COLUMNS:
            assert _bits(loaded[position][column]) == _bits(original[column]), (
                position,
                column,
            )
        assert loaded[position]["EPOCH"] == original["EPOCH"]
    assert math.copysign(1.0, loaded[2]["BSTAR"]) == -1.0
    assert loaded[3]["BSTAR"] == 5e-324


def test_replay_projection_omits_derived_columns(tmp_path):
    """``EPOCH_JD`` and ``SEMIMAJOR_AXIS`` are recomputed, so they are not written.

    A second copy on disk is one a later edit can silently contradict.
    """
    record = make_omm(A, OMM_EPOCH, EPOCH_JD=123.0, SEMIMAJOR_AXIS=999.0)
    directory = _directory(tmp_path)
    _, records_path = save_replay_orbits(directory, [A], [record])

    payload = json.loads(Path(records_path).read_text())
    assert "EPOCH_JD" not in payload
    assert "SEMIMAJOR_AXIS" not in payload

    _, loaded = load_replay_orbits(directory, allow_missing_checksum=False)
    derived = record_elements(loaded[0])
    expected = record_elements({k: v for k, v in record.items() if k != "EPOCH_JD"})
    assert _bits(derived["SEMIMAJOR_AXIS"]) == _bits(expected["SEMIMAJOR_AXIS"])
    assert derived["EPOCH_JD"] == expected["EPOCH_JD"]


def test_mixed_replay_is_standard_json(tmp_path):
    """A cell one kind has and the other does not is ``null``, never ``NaN``."""

    def reject(literal):
        raise AssertionError(f"{literal} is not a value standard JSON may carry")

    directory = _directory(tmp_path)
    _, records_path = save_replay_orbits(
        directory,
        [26867, A],
        [make_tle_record(26867, TLE_EPOCH), make_omm(A, OMM_EPOCH)],
    )

    payload = json.loads(Path(records_path).read_text(), parse_constant=reject)

    assert payload["MEAN_MOTION"]["0"] is None
    assert payload["TLE_LINE1"]["1"] is None
    assert payload["NORAD_CAT_ID"] == {"0": 26867, "1": A}


@pytest.mark.parametrize(
    "record",
    [
        make_omm(A, OMM_EPOCH, MEAN_MOTION=None),
        make_omm(A, OMM_EPOCH, MEAN_MOTION=float("nan")),
        make_omm(A, OMM_EPOCH, MEAN_MOTION=float("inf")),
        make_omm(A, OMM_EPOCH, MEAN_MOTION=float("-inf")),
        make_omm(A, OMM_EPOCH, EPOCH=None),
        make_tle_record(A, TLE_EPOCH, DATA_SOURCE=float("inf")),
        make_tle_record(A, TLE_EPOCH, OBJECT_NAME={"not": "a scalar"}),
    ],
    ids=[
        "required value missing",
        "required NaN",
        "required infinity",
        "required negative infinity",
        "missing epoch",
        "optional infinity",
        "unsupported scalar",
    ],
)
def test_required_nonfinite_or_missing_values_fail_before_write(tmp_path, record):
    """Nothing is written until the whole pair validates and serialises.

    A half-written pair is worse than none: the two files describe one selection
    and a reader that finds them disagreeing has no way to tell which is right.
    """
    directory = _directory(tmp_path)
    ids_path, records_path = _write_pair(directory, "sentinel ids\n", "sentinel records")

    with pytest.raises((ValueError, TypeError)):
        save_replay_orbits(directory, [A], [record])

    assert ids_path.read_text() == "sentinel ids\n"
    assert records_path.read_text() == "sentinel records"


# ---------------------------------------------------------------------------
# Alignment and identity on the way out
# ---------------------------------------------------------------------------

def test_save_alignment_and_id_validation(tmp_path):
    """The IDs and the records are matched one to one, and never repaired."""
    record = make_tle_record(A, TLE_EPOCH)
    path = tmp_path / "used_orbits.json"

    # ``zip`` would truncate and write a file describing different satellites.
    with pytest.raises(ValueError, match="align"):
        save_orbits_for_reuse(path, [A, B], [record])
    with pytest.raises(ValueError, match="align"):
        save_orbits_for_reuse(path, [A], [record, make_tle_record(B, TLE_EPOCH)])

    # The record's own identity has to agree with the ID it is filed against.
    with pytest.raises(ValueError):
        save_orbits_for_reuse(path, [B], [record])
    with pytest.raises(ValueError):
        save_orbits_for_reuse(
            path, [B], [dict(record, NORAD_CAT_ID=B)]  # lines still belong to A
        )
    with pytest.raises(ValueError):
        save_orbits_for_reuse(
            path, [A], [{k: v for k, v in record.items() if k != "NORAD_CAT_ID"}]
        )

    # A supplied ID is validated, not cast: 25544.5 truncates to a real, wrong
    # satellite and then looks perfectly aligned with its record.
    with pytest.raises(ValueError):
        save_orbits_for_reuse(path, ["25544.5"], [record])
    with pytest.raises(ValueError):
        save_orbits_for_reuse(path, [True], [record])

    # An exact decimal spelling is the same catalogue number and is accepted.
    save_orbits_for_reuse(path, ["25544.0"], [record])
    assert json.loads(path.read_text())["NORAD_CAT_ID"] == {"0": A}

    # A NumPy array must not be truth-tested: that raises rather than answering
    # "is it empty", which turns a satellite-free run into a crash.
    save_orbits_for_reuse(path, np.array([A]), [record])
    assert json.loads(path.read_text())["NORAD_CAT_ID"] == {"0": A}
    save_orbits_for_reuse(path, np.array([]), [])
    assert json.loads(path.read_text()) in ({}, [])


@pytest.mark.parametrize("place", ["aligned", "record"])
@pytest.mark.parametrize(
    "truth", [True, np.bool_(True)], ids=["Python bool", "NumPy bool"]
)
def test_a_boolean_is_never_a_satellite_identity(tmp_path, place, truth):
    """``True`` is an ``Integral`` equal to 1, and NORAD 1 is a real satellite.

    ``norad_id_of`` refuses a Python boolean and cannot refuse a NumPy one —
    that guard belongs to this layer — so it has to cover the record's own
    identity and not only the ID the record is filed against. Repairing either
    into a catalogue number writes a replay of whichever satellite the repair
    happened to name.
    """
    record = make_omm(1, OMM_EPOCH)
    norad_ids, records = (
        ([truth], [record])
        if place == "aligned"
        else ([1], [dict(record, NORAD_CAT_ID=truth)])
    )

    directory = _directory(tmp_path, f"boolean-{place}-{type(truth).__name__}")
    ids_path, records_path = _write_pair(directory, "sentinel ids\n", "sentinel records")
    table = directory / "sentinel-table.json"
    table.write_text("sentinel table")

    with pytest.raises(ValueError):
        save_replay_orbits(directory, norad_ids, records)
    with pytest.raises(ValueError):
        save_orbits_for_reuse(table, norad_ids, records)

    assert ids_path.read_text() == "sentinel ids\n"
    assert records_path.read_text() == "sentinel records"
    assert table.read_text() == "sentinel table"


def test_replay_optional_nullable_metadata_becomes_null(tmp_path):
    """An optional cell that is a pandas null is missing, not unserialisable.

    ``pd.NA`` raises rather than answering a truth test, and reading that as
    "present" hands it to the serialiser, which refuses it — so a record read
    out of a nullable frame, as both consumers' readers produce, could not be
    saved at all.
    """
    absent = make_omm(A, OMM_EPOCH, DATA_SOURCE=pd.NA, ECCENTRICITY=0.0066635)
    present = make_omm(B, OMM_EPOCH, DATA_SOURCE="celestrak")
    directory = _directory(tmp_path)
    _, records_path = save_replay_orbits(directory, [A, B], [absent, present])

    payload = json.loads(Path(records_path).read_text())
    assert payload["DATA_SOURCE"] == {"0": None, "1": "celestrak"}

    _, loaded = load_replay_orbits(directory, allow_missing_checksum=False)
    assert loaded[0].get("DATA_SOURCE") is None
    assert loaded[1]["DATA_SOURCE"] == "celestrak"
    # The elements themselves still make the round trip bit for bit.
    assert _bits(loaded[0]["ECCENTRICITY"]) == _bits(0.0066635)

    # A required element is still required, whichever null it arrives as, and a
    # cell that is not a scalar at all is still not a value a record may hold.
    for unusable in (pd.NA, pd.NaT, None, float("nan"), [1.0]):
        with pytest.raises((ValueError, TypeError)):
            save_orbits_for_reuse(
                directory / "used_orbits.json",
                [A],
                [make_omm(A, OMM_EPOCH, MEAN_MOTION=unusable)],
            )


@pytest.mark.parametrize("null", [pd.NA, None, float("nan")], ids=["pd.NA", "None", "nan"])
def test_a_null_kind_or_status_cell_is_a_widened_frame_not_a_claim(tmp_path, null):
    """A null in a field that says how to read a record is no field at all.

    A frame holding both kinds gives every row every column, so an OMM row
    carries a null ``TLE_CHECKSUM_STATUS`` and either kind may carry a null
    ``RECORD_KIND``. ``pd.NA`` reaches the canonical validator as the string
    ``"<NA>"`` and is refused there as an unknown status — for an OMM, which has
    no status at all — so valid records from a nullable frame could not be saved.
    """
    omm = make_omm(A, OMM_EPOCH, ECCENTRICITY=0.0066635)
    omm["TLE_CHECKSUM_STATUS"] = null
    tle = make_tle_record(B, TLE_EPOCH)
    tle["TLE_CHECKSUM_STATUS"] = null
    tle["RECORD_KIND"] = null

    path = tmp_path / "used_orbits.json"
    save_orbits_for_reuse(path, [A, B], [omm, tle])
    directory = _directory(tmp_path)
    save_replay_orbits(directory, [A, B], [omm, tle])

    _, loaded = load_replay_orbits(directory, allow_missing_checksum=False)
    assert "TLE_CHECKSUM_STATUS" not in loaded[0]
    assert _bits(loaded[0]["ECCENTRICITY"]) == _bits(0.0066635)
    # The TLE said nothing about its checksums, and they verify: that is what
    # it is recorded as, not as whatever the null stringified to.
    assert loaded[1]["TLE_CHECKSUM_STATUS"] == "verified"

    # A status that is actually stated and unknown is still refused.
    claimed = dict(make_tle_record(B, TLE_EPOCH), TLE_CHECKSUM_STATUS="probably-fine")
    with pytest.raises(ValueError):
        save_orbits_for_reuse(path, [B], [claimed])


def test_single_table_duplicates_and_frozen_pair_uniqueness(tmp_path):
    """The single table may repeat a satellite; a frozen pair may not."""
    first = make_tle_record(A, TLE_EPOCH)
    second = make_tle_record(A, TLE_EPOCH + 1.0)
    path = tmp_path / "used_orbits.json"

    save_orbits_for_reuse(path, [A, A], [first, second])
    saved = json.loads(path.read_text())
    assert saved["NORAD_CAT_ID"] == {"0": A, "1": A}
    assert list(saved["TLE_LINE1"].values()) == [first["TLE_LINE1"], second["TLE_LINE1"]]

    directory = _directory(tmp_path)
    with pytest.raises(ValueError):
        save_replay_orbits(directory, [A, A], [first, second])

    # And a pair built by other means with two rows for one saved ID is refused
    # on the way back in: choosing between them would be reselecting the input.
    duplicated = _directory(tmp_path, "duplicated")
    _write_pair(
        duplicated,
        f"{A}\n",
        json.dumps(_columns([_replay_row(A, first), _replay_row(A, second)])),
    )
    with pytest.raises(orbit_input_error()):
        load_replay_orbits(duplicated, allow_missing_checksum=False)


@pytest.mark.parametrize(
    "name,norad_ids,records",
    [
        ("lists", [], []),
        ("array", np.array([]), []),
        ("none", None, None),
        ("tuples", (), ()),
    ],
)
def test_empty_selection_writes_two_explicit_files(tmp_path, name, norad_ids, records):
    """A run that modelled nothing is a state on disk, not a missing file."""
    directory = _directory(tmp_path, f"empty-{name}")
    ids_path, records_path = save_replay_orbits(directory, norad_ids, records)

    assert Path(ids_path).exists() and Path(records_path).exists()
    assert Path(ids_path).read_text().strip() == ""
    assert json.loads(Path(records_path).read_text()) in ({}, [])
    assert load_replay_orbits(directory, allow_missing_checksum=False) == ([], [])


def test_id_file_uses_existing_first_column_grammar(tmp_path):
    """The ID file keeps the grammar it has had, extension notwithstanding.

    It is read as a first column of text: comments, blank lines and trailing
    columns are tolerated. No YAML parser is involved, and none is a dependency.
    """
    directory = _directory(tmp_path)
    rows = [
        _replay_row(A, make_tle_record(A, TLE_EPOCH)),
        _replay_row(B, make_tle_record(B, TLE_EPOCH)),
        _replay_row(C, make_tle_record(C, TLE_EPOCH)),
    ]
    records_text = json.dumps(_columns(rows))
    ids_path, _ = _write_pair(
        directory,
        f"# the satellites this run propagated\n\n{A} ISS (ZARYA)\n{B}.0   # a note\n4.3013e4\n",
        records_text,
    )

    loaded_ids, loaded = load_replay_orbits(directory, allow_missing_checksum=False)
    assert loaded_ids == [A, B, C]
    assert [int(record["NORAD_CAT_ID"]) for record in loaded] == [A, B, C]

    for bad in (f"{A}\n{B}\n{A}\n", f"{A}\n25544.5\n"):
        ids_path.write_text(bad)
        with pytest.raises(orbit_input_error()) as caught:
            load_replay_orbits(directory, allow_missing_checksum=False)
        assert str(ids_path) in str(caught.value)


# ---------------------------------------------------------------------------
# Reading a pair back
# ---------------------------------------------------------------------------

def _good_rows():
    return [
        _replay_row(A, make_tle_record(A, TLE_EPOCH)),
        _replay_row(B, make_tle_record(B, TLE_EPOCH)),
    ]


def _ids_text(*norad_ids):
    return "".join(f"{norad_id}\n" for norad_id in norad_ids)


@pytest.mark.parametrize(
    "prepare",
    [
        lambda d: (d / replay_file_names()[1]).write_text(
            json.dumps(_columns(_good_rows()))
        ),
        lambda d: (d / replay_file_names()[0]).write_text(_ids_text(A, B)),
        lambda d: _write_pair(d, _ids_text(A, B), "{not json"),
        lambda d: _write_pair(d, _ids_text(A), json.dumps(_columns(_good_rows()))),
        lambda d: _write_pair(
            d, _ids_text(A, B, C), json.dumps(_columns(_good_rows()))
        ),
        lambda d: _write_pair(
            d,
            _ids_text(A),
            '{"NORAD_CAT_ID": {"0": 25544}, "NORAD_CAT_ID": {"0": 25544}}',
        ),
        lambda d: _write_pair(
            d, _ids_text(A), '{"NORAD_CAT_ID": {"0": 25544}, "TLE_LINE1": {"1": "x"}}'
        ),
        lambda d: _write_pair(
            d,
            _ids_text(A),
            json.dumps(
                _columns(
                    [
                        {
                            key: value
                            for key, value in _good_rows()[0].items()
                            if key != "NORAD_CAT_ID"
                        }
                    ]
                )
            ),
        ),
        lambda d: _write_pair(
            d,
            _ids_text(A),
            json.dumps(_columns([dict(_good_rows()[0], NORAD_CAT_ID="25544.5")])),
        ),
        lambda d: _write_pair(
            d,
            _ids_text(B),
            json.dumps(_columns([dict(_good_rows()[0], NORAD_CAT_ID=B)])),
        ),
    ],
    ids=[
        "no ID file",
        "no records file",
        "a corrupt records file",
        "a record for an unlisted satellite",
        "a listed satellite with no record",
        "a repeated JSON member",
        "columns describing different rows",
        "a row with no identity",
        "a row with an unusable identity",
        "a row whose lines belong to another satellite",
    ],
)
def test_replay_requires_exact_file_alignment(tmp_path, monkeypatch, prepare):
    """Anything short of exact stops the run; there is no second source to try."""
    forbid_acquisition(monkeypatch)
    forbid_cache(monkeypatch)
    directory = _directory(tmp_path)
    prepare(directory)

    with pytest.raises(orbit_input_error()) as caught:
        load_replay_orbits(directory, allow_missing_checksum=False)
    assert str(directory) in str(caught.value)


@pytest.mark.parametrize(
    "prepare,code",
    [
        (
            lambda d: (d / replay_file_names()[1]).write_text(
                json.dumps(_columns(_good_rows()))
            ),
            INPUT_UNREADABLE,
        ),
        (
            lambda d: (d / replay_file_names()[0]).write_text(_ids_text(A, B)),
            INPUT_UNREADABLE,
        ),
        (lambda d: _write_pair(d, _ids_text(A, B), "{not json"), INPUT_UNREADABLE),
        (
            lambda d: _write_pair(
                d, _ids_text(A, B, A), json.dumps(_columns(_good_rows()))
            ),
            INPUT_STRUCTURE,
        ),
        (
            lambda d: _write_pair(d, _ids_text(A), json.dumps(_columns(_good_rows()))),
            INPUT_STRUCTURE,
        ),
        (
            lambda d: _write_pair(
                d, _ids_text(A, B, C), json.dumps(_columns(_good_rows()))
            ),
            INPUT_STRUCTURE,
        ),
        (
            lambda d: _write_pair(
                d,
                _ids_text(A),
                json.dumps(_columns([_good_rows()[0], _good_rows()[0]])),
            ),
            INPUT_STRUCTURE,
        ),
        (
            lambda d: _write_pair(
                d, f"{A}\n25544.5\n", json.dumps(_columns(_good_rows()))
            ),
            INPUT_IDENTITY,
        ),
        (
            lambda d: _write_pair(
                d,
                _ids_text(A),
                json.dumps(
                    _columns(
                        [
                            {
                                key: value
                                for key, value in _good_rows()[0].items()
                                if key != "NORAD_CAT_ID"
                            }
                        ]
                    )
                ),
            ),
            INPUT_IDENTITY,
        ),
        (
            lambda d: _write_pair(
                d,
                _ids_text(A),
                json.dumps(_columns([dict(_good_rows()[0], NORAD_CAT_ID="25544.5")])),
            ),
            INPUT_IDENTITY,
        ),
        (
            lambda d: _write_pair(
                d,
                _ids_text(B),
                json.dumps(_columns([dict(_good_rows()[0], NORAD_CAT_ID=B)])),
            ),
            INPUT_IDENTITY,
        ),
    ],
    ids=[
        "no ID file",
        "no records file",
        "a corrupt records file",
        "a satellite listed twice",
        "a record for an unlisted satellite",
        "a listed satellite with no record",
        "two records for one satellite",
        "an ID line that is not an ID",
        "a row with no identity",
        "a row with an unusable identity",
        "a row whose lines belong to another satellite",
    ],
)
def test_replay_failures_say_which_kind_of_failure_they_are(tmp_path, prepare, code):
    """Each refusal carries its category, so no caller has to read the message.

    None of these is a record the checksum opt-in would admit — a satellite
    listed twice and a listed satellite with no record least of all — and an
    application that suggested it here would be sending the user after a setting
    that cannot repair the file.
    """
    directory = _directory(tmp_path)
    prepare(directory)

    with pytest.raises(orbit_input_error()) as caught:
        load_replay_orbits(directory, allow_missing_checksum=False)

    assert caught.value.code == code


def test_replay_undecodable_id_file_has_context(tmp_path):
    """Bytes that are not text fail like any other unreadable input, and say so.

    Decoding is part of reading a file, so it cannot be the one failure that
    escapes the contextual error the caller catches: an application left with a
    bare ``UnicodeDecodeError`` has neither the path nor the reason to report.
    """
    directory = _directory(tmp_path)
    save_replay_orbits(directory, [A], [make_tle_record(A, TLE_EPOCH)])
    ids_path = directory / replay_file_names()[0]
    ids_path.write_bytes(b"\xff\n")

    with pytest.raises(orbit_input_error()) as caught:
        load_replay_orbits(directory, allow_missing_checksum=False)

    assert str(caught.value.path) == str(ids_path)
    assert str(ids_path) in str(caught.value)
    assert isinstance(caught.value.__cause__, UnicodeDecodeError)


def test_replay_reads_only_named_files(tmp_path, monkeypatch):
    """Two files, named. Nothing scans the directory and nothing else is read."""
    directory = _directory(tmp_path)
    _write_pair(directory, _ids_text(A, B), json.dumps(_columns(_good_rows())))
    (directory / "zzz-unrelated.json").write_text("{not json")

    forbid_acquisition(monkeypatch)
    forbid_cache(monkeypatch)
    monkeypatch.setattr(client, "search_satellites", forbid("search_satellites"))
    monkeypatch.setattr(Path, "glob", forbid("Path.glob"))
    monkeypatch.setattr(glob_module, "glob", forbid("glob.glob"))

    loaded_ids, loaded = load_replay_orbits(directory, allow_missing_checksum=False)

    assert loaded_ids == [A, B]
    assert len(loaded) == 2


def test_replay_never_reselects_by_age(tmp_path, monkeypatch):
    """Saved records are used however far their epochs are from the observation.

    That is what makes them the same records. Re-selecting the nearest one would
    make the replay a different run.
    """
    ancient = [
        make_tle_record(A, jd(2001, 7, 4)),
        make_tle_record(B, jd(2001, 7, 4)),
    ]
    directory = _directory(tmp_path)
    save_replay_orbits(directory, [A, B], ancient)

    forbid_acquisition(monkeypatch)
    forbid_cache(monkeypatch)
    monkeypatch.setattr(resolve_module(), "resolve_orbits", forbid("resolve_orbits"))

    loaded_ids, loaded = load_replay_orbits(directory, allow_missing_checksum=False)

    assert loaded_ids == [A, B]
    assert [record["TLE_LINE1"] for record in loaded] == [
        record["TLE_LINE1"] for record in ancient
    ]


def test_unverified_replay_requires_explicit_policy_every_time(tmp_path):
    """Provenance survives the save, so the opt-in is needed on every pass."""
    epoch = jd(2017, 5, 14)
    stripped = make_tle_record(
        A, epoch, TLE_LINE1=NO_CHECKSUM_PAIR[0], TLE_LINE2=NO_CHECKSUM_PAIR[1]
    )
    line1, line2 = (with_checksum(line) for line in make_tle(B, epoch))
    rechecksummed = make_tle_record(
        B,
        epoch,
        TLE_LINE1=line1,
        TLE_LINE2=line2,
        **{CHECKSUM_STATUS_FIELD: CHECKSUM_UNVERIFIED_MISSING},
    )
    directory = _directory(tmp_path)
    save_replay_orbits(directory, [A, B], [stripped, rechecksummed])

    with pytest.raises(orbit_input_error()) as caught:
        load_replay_orbits(directory, allow_missing_checksum=False)

    # The one category the opt-in below would lift, and it says so rather than
    # leaving the caller to recognise the sentence about it.
    assert caught.value.code == INPUT_CHECKSUM_POLICY

    _, loaded = load_replay_orbits(directory, allow_missing_checksum=True)
    assert [record[CHECKSUM_STATUS_FIELD] for record in loaded] == [
        CHECKSUM_UNVERIFIED_MISSING,
        CHECKSUM_UNVERIFIED_MISSING,
    ]
    assert loaded[0]["TLE_LINE1"] == NO_CHECKSUM_PAIR[0]
    assert loaded[1]["TLE_LINE1"] == line1

    # The policy has to be stated: there is no safe default for it.
    with pytest.raises(TypeError):
        load_replay_orbits(directory)


@pytest.mark.parametrize("allow", [False, True])
@pytest.mark.parametrize("defect", ["unknown_status", "corrupt_checksum"])
def test_an_unknown_status_or_corrupt_checksum_is_always_refused(
    tmp_path, defect, allow
):
    """An unrecognised claim is not "no claim", and a wrong digit is never allowed.

    Allowing missing checksums must not weaken what a *present* one means.
    """
    epoch = jd(2017, 5, 14)
    line1, line2 = make_tle(A, epoch)
    overrides = {}
    if defect == "unknown_status":
        overrides[CHECKSUM_STATUS_FIELD] = "probably-fine"
    else:
        line1 = line1[:68] + str((int(line1[68]) + 1) % 10)
    row = _replay_row(
        A,
        make_tle_record(A, epoch, TLE_LINE1=line1, TLE_LINE2=line2, **overrides),
    )
    directory = _directory(tmp_path, f"{defect}-{allow}")
    _write_pair(directory, _ids_text(A), json.dumps(_columns([row])))

    with pytest.raises(orbit_input_error()) as caught:
        load_replay_orbits(directory, allow_missing_checksum=allow)

    # The record itself, under either policy — never the category whose remedy
    # is the opt-in that is already on in half of these runs.
    assert caught.value.code == INPUT_INVALID_RECORD


def test_a_claimed_verified_status_cannot_launder_checksum_less_lines(tmp_path):
    """Checksum-less lines that claim to be verified come back unverified.

    The claim is not evidence: nothing in the record can verify the digits its
    source omitted. A permissive replay still needs the opt-in for it.
    """
    row = _replay_row(
        A,
        make_tle_record(
            A,
            jd(2017, 5, 14),
            TLE_LINE1=NO_CHECKSUM_PAIR[0],
            TLE_LINE2=NO_CHECKSUM_PAIR[1],
            **{CHECKSUM_STATUS_FIELD: CHECKSUM_VERIFIED},
        ),
    )
    directory = _directory(tmp_path, "claimed-verified")
    _write_pair(directory, _ids_text(A), json.dumps(_columns([row])))

    with pytest.raises(orbit_input_error()):
        load_replay_orbits(directory, allow_missing_checksum=False)

    _, loaded = load_replay_orbits(directory, allow_missing_checksum=True)
    assert loaded[0][CHECKSUM_STATUS_FIELD] == CHECKSUM_UNVERIFIED_MISSING


# ---------------------------------------------------------------------------
# Compatibility in both directions
# ---------------------------------------------------------------------------

def test_old_tabascal_tables_remain_readable(tmp_path):
    """The tables tabascal writes today read through the strict route unchanged.

    They carry no checksum-status field and no fetch time, which is not a defect
    — there is nothing to convert, and nothing to invent.
    """
    directory = _directory(tmp_path)
    line1, line2 = make_tle(A, TLE_EPOCH)
    pd.DataFrame(
        [
            {
                "NORAD_CAT_ID": A,
                "RECORD_KIND": "tle",
                "OBJECT_NAME": f"SAT-{A}",
                "TLE_LINE1": line1,
                "TLE_LINE2": line2,
            }
        ]
    ).to_json(directory / "used_orbits_tle.json")

    omm = make_omm(B, OMM_EPOCH)
    pd.DataFrame(
        [
            {
                "NORAD_CAT_ID": B,
                "RECORD_KIND": "omm",
                "OBJECT_NAME": omm["OBJECT_NAME"],
                "OBJECT_ID": omm["OBJECT_ID"],
                "EPOCH": omm["EPOCH"],
                **{column: omm[column] for column in OMM_ELEMENT_COLUMNS},
            }
        ]
    ).to_json(directory / "used_orbits_omm.json")

    assert len(read_orbit_file(directory / "used_orbits_tle.json")) == 1
    frame = read_extra_orbit_dir(directory)
    assert frame["NORAD_CAT_ID"].tolist() == [B, A]

    resolution = resolve_orbits(
        [A, B],
        TLE_EPOCH,
        log=lambda _m: None,
        **policy(
            source_order=(GROUP_EXTRA,),
            extra_orbit_max_age_days=None,
            endpoints=(ForbiddenEndpoint().pair,),
        ),
        extra_records=frame,
    )
    assert resolution.complete
    assert resolution.resolved[A].record[CHECKSUM_STATUS_FIELD] == CHECKSUM_VERIFIED
    # ``to_json`` already rounded the OMM elements when tabascal wrote them; the
    # precision they had before that save cannot be recovered and is not claimed.
    assert resolution.resolved[B].record["ECCENTRICITY"] == pytest.approx(
        omm["ECCENTRICITY"], abs=1e-9
    )


def test_new_standard_tables_remain_legacy_readable(tmp_path):
    """A file this writer produces still reads through the forgiving scan."""
    directory = _directory(tmp_path)
    tle = make_tle_record(A, TLE_EPOCH)
    omm = make_omm(B, OMM_EPOCH)
    save_orbits_for_reuse(directory / "used_orbits.json", [A, B], [tle, omm])

    loaded = read_legacy_tle_records(directory)

    assert loaded["NORAD_CAT_ID"].tolist() == [A, B]
    assert loaded.loc[0, "TLE_LINE1"] == tle["TLE_LINE1"]
    derived = record_elements(loaded.loc[1])
    expected = record_elements(omm)
    for column in OMM_ELEMENT_COLUMNS:
        assert derived[column] == expected[column], column


def test_replay_io_failure_is_reported(tmp_path, monkeypatch):
    """An I/O failure is reported as one. There is nothing to fall back to."""
    directory = _directory(tmp_path)
    records = [make_tle_record(A, TLE_EPOCH)]
    save_replay_orbits(directory, [A], records)

    real_read_text = Path.read_text

    def failing_read(self, *args, **kwargs):
        if str(self).startswith(str(directory)):
            raise OSError("input/output error")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing_read)
    with pytest.raises((orbit_input_error(), OSError)) as caught:
        load_replay_orbits(directory, allow_missing_checksum=False)
    assert "input/output error" in str(caught.value)
    monkeypatch.undo()

    # Injected rather than a read-only directory: root may write to one of
    # those, and the failure this asserts would simply not happen.
    unwritable = _directory(tmp_path, "unwritable")
    real_write_text = Path.write_text

    def failing_write(self, *args, **kwargs):
        if str(self).startswith(str(unwritable)):
            raise OSError("read-only file system")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing_write)
    with pytest.raises((OSError, orbit_input_error())) as caught:
        save_replay_orbits(unwritable, [A], records)
    assert "read-only file system" in str(caught.value)
    assert not list(unwritable.iterdir())


def test_a_saved_pair_is_not_a_managed_cache_envelope(tmp_path):
    """The replay format stays an explicit orbit table, readable as one."""
    directory = _directory(tmp_path)
    _, records_path = save_replay_orbits(
        directory, [A], [make_tle_record(A, TLE_EPOCH)]
    )
    payload = json.loads(Path(records_path).read_text())
    assert "schema_version" not in payload and "records" not in payload
    assert len(read_orbit_file(records_path)) == 1

    managed = TextOrbitCache(tmp_path / "cache")
    managed.store(A, pd.DataFrame([make_tle_record(A, TLE_EPOCH)]))
    assert json.loads(managed.path(A).read_text())["schema_version"] == 2
