"""Offline tests for the per-NORAD immutable-record cache."""

import json

import pandas as pd
import pytest

from datetime import datetime, timedelta, timezone

from satchecker_client.cache import (
    CacheValidationError,
    SCHEMA_VERSION,
    SearchSnapshot,
    TextOrbitCache,
    read_legacy_tle_records,
)
from satchecker_client.client import SEARCH_COLUMNS
from satchecker_client.records import record_epoch_jd
from satchecker_client.tle_parse import tle_checksum

from .tle_helpers import (  # noqa: F401
    NO_CHECKSUM_PAIR,
    STRAY_BACKSLASH_PAIR,
    block_network,
    jd,
    make_catalogue_df,
    make_omm_catalogue_df,
)


EPOCH = jd(2023, 1, 1)


def _native(value):
    """A DataFrame cell as a plain Python scalar, for exact json round-trips."""
    return value.item() if hasattr(value, "item") else value


def test_store_and_get_round_trip(tmp_path):
    cache = TextOrbitCache(tmp_path)
    cache.store(25544, make_catalogue_df([(25544, EPOCH)]))
    loaded = cache.get(25544)
    assert len(loaded) == 1
    assert loaded.loc[0, "NORAD_CAT_ID"] == 25544


def test_records_at_distinct_epochs_are_merged(tmp_path):
    cache = TextOrbitCache(tmp_path)
    cache.store(25544, make_catalogue_df([(25544, EPOCH - 1)]))
    cache.store(25544, make_catalogue_df([(25544, EPOCH + 1)]))
    assert len(cache.get(25544)) == 2


def test_identical_records_are_deduplicated(tmp_path):
    cache = TextOrbitCache(tmp_path)
    record = make_catalogue_df([(25544, EPOCH)])
    cache.store(25544, record)
    cache.store(25544, record)
    assert len(cache.get(25544)) == 1


def test_wrong_satellite_cannot_be_stored(tmp_path):
    cache = TextOrbitCache(tmp_path)
    try:
        cache.store(25544, make_catalogue_df([(43013, EPOCH)]))
    except ValueError as error:
        assert "another satellite" in str(error)
    else:
        raise AssertionError("wrong-satellite record was cached")


def test_corrupt_or_wrong_schema_file_is_a_cache_miss(tmp_path):
    cache = TextOrbitCache(tmp_path)
    cache.path(25544).write_text("not-json")
    assert cache.get(25544).empty
    cache.path(25544).write_text(json.dumps({"schema_version": SCHEMA_VERSION + 1}))
    assert cache.get(25544).empty


def test_unusable_file_is_reported_but_an_absent_one_is_not(tmp_path):
    """A cache that never takes hold must not be invisible.

    Silently re-fetching every run gives the user nothing to debug, so an
    existing-but-unusable file warns while a plain miss stays quiet.
    """
    cache = TextOrbitCache(tmp_path)
    messages = []

    assert cache.get(25544, log=messages.append).empty
    assert messages == []

    cache.path(25544).write_text("not-json")
    assert cache.get(25544, log=messages.append).empty
    assert len(messages) == 1
    assert "unusable" in messages[0]
    assert str(cache.path(25544)) in messages[0]


def test_write_is_an_envelope_not_a_pandas_orientation(tmp_path):
    cache = TextOrbitCache(tmp_path)
    cache.store(25544, make_catalogue_df([(25544, EPOCH)]))
    payload = json.loads(cache.path(25544).read_text())
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["norad_id"] == 25544
    assert isinstance(payload["records"], list)


# ---------------------------------------------------------------------------
# Schema v2: two kinds in one file
# ---------------------------------------------------------------------------

class TestMixedKindCache:
    """A satellite's file spans the archive handover, so it holds both kinds.

    Almost everything the cache does had one implicit assumption in it — that
    every record has TLE lines. Concatenating an OMM record onto a TLE frame
    widens the columns and fills the gaps with nulls, so any check that looks at
    a whole column rather than a row now sees a defect that is not one.
    """

    def test_both_kinds_coexist_for_one_satellite(self, tmp_path):
        cache = TextOrbitCache(tmp_path)
        cache.store(25544, make_catalogue_df([(25544, EPOCH)]))
        cache.store(25544, make_omm_catalogue_df([(25544, EPOCH + 40)]))
        loaded = cache.get(25544)
        assert len(loaded) == 2
        assert set(loaded["RECORD_KIND"]) == {"tle", "omm"}

    def test_an_omm_record_round_trips_alone(self, tmp_path):
        cache = TextOrbitCache(tmp_path)
        cache.store(25544, make_omm_catalogue_df([(25544, EPOCH)]))
        loaded = cache.get(25544)
        assert len(loaded) == 1
        assert record_epoch_jd(loaded.loc[0]) == pytest.approx(EPOCH, abs=1e-6)

    def test_omm_elements_survive_the_json_file_exactly(self, tmp_path):
        # Same hazard as the rank broadcast: json writes a float through repr,
        # which round-trips exactly, but only if it goes as a number.
        cache = TextOrbitCache(tmp_path)
        stored = make_omm_catalogue_df([(25544, EPOCH)])
        cache.store(25544, stored)
        loaded = cache.get(25544)
        for column in (
            "INCLINATION",
            "RA_OF_ASC_NODE",
            "ECCENTRICITY",
            "ARG_OF_PERICENTER",
            "MEAN_ANOMALY",
            "MEAN_MOTION",
            "BSTAR",
        ):
            assert loaded.loc[0, column] == stored.loc[0, column], column

    def test_omm_elements_survive_a_replay_file_exactly(self, tmp_path):
        """The legacy reader must decode each float to the double that was written.

        ``pandas.read_json`` defaults to a fast float parser that is not
        correctly rounded: it reads an eccentricity of 0.0066635 back as
        0.006663499999999999, and a BSTAR of 3.2e-05 as 3.2000000000000005e-05.
        Each is a different double, so a replayed trajectory quietly disagrees
        with the run whose records the file was written to reproduce. Both
        values are ordinary, and each fails on its own without the fix.

        The file is built the way a replay writer must build one — stdlib
        ``json``, column-oriented, floats through ``repr`` — because
        ``DataFrame.to_json`` rounds to a fixed number of decimal places and
        would corrupt these values on the way *out*, before the reader is even
        reached. The hazard under test is the read.
        """
        sentinels = {"ECCENTRICITY": 0.0066635, "BSTAR": 3.2e-05}
        stored = make_omm_catalogue_df([(25544, EPOCH)])
        for column, value in sentinels.items():
            stored.loc[0, column] = value
        # .item() unwraps the NumPy scalars a DataFrame cell yields; json
        # cannot serialise those, and a ``default=str`` fallback would write
        # them as strings instead.
        payload = {
            column: {"0": _native(stored.loc[0, column])}
            for column in stored.columns
        }
        text = json.dumps(payload)
        (tmp_path / "replay.json").write_text(text)

        # The premise: each sentinel reaches the file as a JSON *number*. A
        # numeric string would be converted by pandas after parsing, bypassing
        # the float parser under test, and the assertions below could then
        # pass without exercising it.
        written = json.loads(text)
        for column, value in sentinels.items():
            assert type(written[column]["0"]) is float, column
            assert written[column]["0"] == value, column

        loaded = read_legacy_tle_records(tmp_path)

        for column, value in sentinels.items():
            assert loaded.loc[0, column] == value, column

    def test_omm_records_dedupe_on_epoch_not_on_absent_lines(self, tmp_path):
        # Deduping on the TLE lines would collapse every OMM record into one,
        # because they all share the same null lines.
        cache = TextOrbitCache(tmp_path)
        cache.store(25544, make_omm_catalogue_df([(25544, EPOCH)]))
        cache.store(25544, make_omm_catalogue_df([(25544, EPOCH + 1)]))
        cache.store(25544, make_omm_catalogue_df([(25544, EPOCH + 2)]))
        assert len(cache.get(25544)) == 3

    def test_identical_omm_records_are_still_deduplicated(self, tmp_path):
        cache = TextOrbitCache(tmp_path)
        record = make_omm_catalogue_df([(25544, EPOCH)])
        cache.store(25544, record)
        cache.store(25544, record)
        assert len(cache.get(25544)) == 1

    def test_tle_dedupe_is_unaffected_by_the_presence_of_omm_rows(self, tmp_path):
        cache = TextOrbitCache(tmp_path)
        tle = make_catalogue_df([(25544, EPOCH)])
        cache.store(25544, tle)
        cache.store(25544, make_omm_catalogue_df([(25544, EPOCH + 40)]))
        cache.store(25544, tle)
        loaded = cache.get(25544)
        assert len(loaded) == 2
        assert list(loaded["RECORD_KIND"]).count("tle") == 1

    def test_a_corrupted_omm_element_makes_the_file_a_miss(self, tmp_path):
        cache = TextOrbitCache(tmp_path)
        cache.store(25544, make_omm_catalogue_df([(25544, EPOCH)]))
        payload = json.loads(cache.path(25544).read_text())
        payload["records"][0]["INCLINATION"] = 999.0
        cache.path(25544).write_text(json.dumps(payload))
        messages = []
        assert cache.get(25544, log=messages.append).empty
        assert "inclination out of range" in messages[0]

    def test_a_wrong_satellite_omm_record_cannot_be_stored(self, tmp_path):
        cache = TextOrbitCache(tmp_path)
        with pytest.raises(ValueError, match="another satellite"):
            cache.store(25544, make_omm_catalogue_df([(43013, EPOCH)]))


class TestSchemaVersionBump:

    def test_the_schema_version_is_two(self):
        assert SCHEMA_VERSION == 2

    def test_a_v1_file_self_evicts_with_a_warning(self, tmp_path):
        # The whole migration path. A v1 envelope is structurally fine and its
        # records are valid TLEs — it is rejected purely on version, warned
        # about, and replaced by the next fetch. Nothing converts it.
        cache = TextOrbitCache(tmp_path)
        v1 = {
            "schema_version": 1,
            "norad_id": 25544,
            "records": make_catalogue_df([(25544, EPOCH)]).to_dict(orient="records"),
        }
        cache.path(25544).write_text(json.dumps(v1))

        messages = []
        assert cache.get(25544, log=messages.append).empty
        assert len(messages) == 1
        assert "unusable" in messages[0]
        assert "schema_version 1" in messages[0]

    def test_the_next_fetch_replaces_the_evicted_file(self, tmp_path):
        cache = TextOrbitCache(tmp_path)
        cache.path(25544).write_text(
            json.dumps({"schema_version": 1, "norad_id": 25544, "records": []})
        )
        cache.store(25544, make_catalogue_df([(25544, EPOCH)]))
        assert json.loads(cache.path(25544).read_text())["schema_version"] == 2
        assert len(cache.get(25544)) == 1


def test_concurrent_stores_do_not_lose_records(tmp_path):
    """Read/merge/write transactions must serialise, not last-writer-wins."""
    import threading

    cache = TextOrbitCache(tmp_path)
    epochs = [EPOCH - i for i in range(8)]
    barrier = threading.Barrier(len(epochs))
    failures = []

    def store(epoch):
        try:
            barrier.wait()
            cache.store(25544, make_catalogue_df([(25544, epoch)]))
        except Exception as error:  # pragma: no cover — failure reporting only
            failures.append(error)

    threads = [threading.Thread(target=store, args=(epoch,)) for epoch in epochs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not failures
    assert len(cache.get(25544)) == len(epochs)


def test_a_tilde_cache_path_is_expanded(tmp_path, monkeypatch):
    """The README writes TextOrbitCache("~/...") and it must mean the home dir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cache = TextOrbitCache("~/orbits")
    cache.store(25544, make_catalogue_df([(25544, EPOCH)]))
    assert (tmp_path / "orbits" / "orbit-25544.json").exists()


@pytest.mark.parametrize("row_id", ["25544", "25544.0", 25544.0])
def test_a_valid_row_id_in_another_form_still_leaves_the_record_uncached(tmp_path, row_id):
    # Not an error: the ID is valid, and the record is left out only because
    # nothing verifies its lines.
    cache = TextOrbitCache(tmp_path)
    frame = _with_lines(25544, NO_CHECKSUM_PAIR)
    frame["NORAD_CAT_ID"] = pd.Series([row_id], dtype=object)
    cache.store(25544, frame)
    assert not cache.path(25544).exists()


def test_records_without_checksums_are_never_written_to_the_cache(tmp_path):
    # The file is shared with other applications and with 0.1.x, which rejects a
    # whole file over one line it cannot validate and then overwrites it.
    cache = TextOrbitCache(tmp_path)
    verified = make_catalogue_df([(25544, EPOCH)])
    unverifiable = make_catalogue_df([(25544, EPOCH + 5)])
    unverifiable.loc[0, ["TLE_LINE1", "TLE_LINE2"]] = list(NO_CHECKSUM_PAIR)

    cache.store(25544, pd.concat([verified, unverifiable], ignore_index=True))

    stored = json.loads(cache.path(25544).read_text())["records"]
    assert [record["TLE_LINE1"] for record in stored] == [verified.loc[0, "TLE_LINE1"]]


def test_a_store_of_only_unverifiable_records_leaves_the_file_alone(tmp_path):
    cache = TextOrbitCache(tmp_path)
    unverifiable = make_catalogue_df([(25544, EPOCH)])
    unverifiable.loc[0, ["TLE_LINE1", "TLE_LINE2"]] = list(NO_CHECKSUM_PAIR)

    cache.store(25544, unverifiable)
    assert not cache.path(25544).exists()

    cache.store(25544, make_catalogue_df([(25544, EPOCH)]))
    before = cache.path(25544).read_text()
    cache.store(25544, unverifiable)
    assert cache.path(25544).read_text() == before


def test_a_repaired_backslash_record_is_written_as_a_standard_line(tmp_path):
    # So a strict reader, 0.1.x included, can still read the file.
    cache = TextOrbitCache(tmp_path)
    frame = make_catalogue_df([(26867, EPOCH)])
    frame.loc[0, ["TLE_LINE1", "TLE_LINE2"]] = list(STRAY_BACKSLASH_PAIR)

    cache.store(26867, frame)

    (record,) = json.loads(cache.path(26867).read_text())["records"]
    for column in ("TLE_LINE1", "TLE_LINE2"):
        line = record[column]
        assert len(line) == 69 and int(line[68]) == tle_checksum(line)


def _with_lines(norad_id, pair):
    frame = make_catalogue_df([(norad_id, EPOCH)])
    frame.loc[0, ["TLE_LINE1", "TLE_LINE2"]] = list(pair)
    return frame


def _renumbered(pair, norad_id):
    """Checksum-less lines rewritten to carry *norad_id*; no checksum to recompute."""
    return tuple(line[:2] + f"{norad_id:05d}" + line[7:] for line in pair)


def _shifted_no_checksum_pair():
    # 68 columns like an honest checksum-less line, but a character is missing
    # mid-line: invalid even when missing checksums are allowed.
    line1, line2 = NO_CHECKSUM_PAIR
    return line1, line2[:20] + line2[21:] + "0"


def _wrong_checksum_backslash_pair():
    line1, line2 = STRAY_BACKSLASH_PAIR
    return line1[:68] + str((int(line1[68]) + 1) % 10) + "\\", line2


@pytest.mark.parametrize(
    "build, norad_id",
    [
        # Malformed input must fail the way it did in 0.1.2, not with whatever
        # the archive-defect handling happens to raise on the way.
        (lambda: make_catalogue_df([(25544, EPOCH)]).assign(TLE_LINE1=None), 25544),
        (lambda: make_catalogue_df([(25544, EPOCH)]).drop(columns=["TLE_LINE2"]), 25544),
        (lambda: make_catalogue_df([(25544, EPOCH)]).assign(RECORD_KIND="bogus"), 25544),
        (lambda: _with_lines(26867, _wrong_checksum_backslash_pair()), 26867),
        # Not valid even allowing for a missing checksum, so not merely skipped.
        (lambda: _with_lines(25544, _shifted_no_checksum_pair()), 25544),
        (lambda: _with_lines(26867, NO_CHECKSUM_PAIR), 26867),  # ISS lines under another ID
        # The row's own ID matters as well as the one in its lines.
        (lambda: _with_lines(26867, NO_CHECKSUM_PAIR), 25544),
        (lambda: _with_lines(25544, NO_CHECKSUM_PAIR).assign(NORAD_CAT_ID=None), 25544),
        (lambda: _with_lines(25544, NO_CHECKSUM_PAIR).drop(columns=["NORAD_CAT_ID"]), 25544),
        (lambda: _with_lines(25544, NO_CHECKSUM_PAIR).assign(NORAD_CAT_ID=25544.5), 25544),
        (lambda: _with_lines(25544, STRAY_BACKSLASH_PAIR), 26867),
        # Row IDs the store's own ID rules reject, however the lines read.
        (lambda: _with_lines(25544, NO_CHECKSUM_PAIR).assign(NORAD_CAT_ID="25_544"), 25544),
        (
            lambda: _with_lines(25544, NO_CHECKSUM_PAIR).assign(
                NORAD_CAT_ID="\uff12\uff15\uff15\uff14\uff14"
            ),
            25544,
        ),
        (lambda: _with_lines(0, _renumbered(NO_CHECKSUM_PAIR, 0)).assign(NORAD_CAT_ID=0), 0),
        (lambda: _with_lines(0, _renumbered(NO_CHECKSUM_PAIR, 0)).assign(NORAD_CAT_ID=False), 0),
        # One bad row fails the whole store rather than quietly dropping out of it.
        (
            lambda: pd.concat(
                [make_catalogue_df([(25544, EPOCH)]), _with_lines(26867, NO_CHECKSUM_PAIR)],
                ignore_index=True,
            ),
            25544,
        ),
    ],
    ids=[
        "null line",
        "missing line column",
        "unknown record kind",
        "backslash with a wrong checksum",
        "checksum-less line missing a character",
        "checksum-less record for another satellite",
        "checksum-less record under another row ID",
        "checksum-less record with a null row ID",
        "checksum-less record with no row ID column",
        "checksum-less record with a fractional row ID",
        "backslash record under another row ID",
        "checksum-less record with an underscored row ID",
        "checksum-less record with a full-width row ID",
        "checksum-less record with row ID 0",
        "checksum-less record with row ID False",
        "mixed batch with one misfiled checksum-less row",
    ],
)
def test_invalid_records_still_raise_cache_validation_error(tmp_path, build, norad_id):
    cache = TextOrbitCache(tmp_path)
    with pytest.raises(CacheValidationError):
        cache.store(norad_id, build())
    assert not cache.path(norad_id).exists()


# ---------------------------------------------------------------------------
# Catalogue searches, cached beside the orbit files
# ---------------------------------------------------------------------------

def _search_frame(*rows):
    """A search_satellites result; rows are (id, name, launch, decay) plus nulls."""
    return pd.DataFrame(
        [
            {
                "NORAD_CAT_ID": norad_id,
                "OBJECT_NAME": name,
                "OBJECT_ID": "2025-119D" if name.startswith("STARLINK-11691") else None,
                "OBJECT_TYPE": None,
                "RCS_SIZE": None,
                "LAUNCH_DATE": launch,
                "DECAY_DATE": decay,
            }
            for norad_id, name, launch, decay in rows
        ],
        columns=SEARCH_COLUMNS,
    )


FETCHED = datetime(2026, 9, 16, 12, 30, 5, tzinfo=timezone.utc)

STARLINK = _search_frame(
    (64236, "STARLINK-11691", "2025-06-03", None),
    (64236, "STARLINK-11691 [DTC]", None, None),  # alias row, fewer fields
    (47406, "STARLINK-2133", None, "2026-01-30"),  # decayed, still in the result
)


class TestSearchCache:
    def test_a_search_round_trips_whole_with_its_fetch_time(self, tmp_path):
        cache = TextOrbitCache(tmp_path)
        cache.store_search("STARLINK", STARLINK, fetched_at=FETCHED)

        snapshot = cache.get_search("STARLINK")

        assert isinstance(snapshot, SearchSnapshot)
        assert snapshot.name == "STARLINK"
        assert snapshot.fetched_at == FETCHED
        found = snapshot.found
        assert list(found.columns) == SEARCH_COLUMNS
        assert found["NORAD_CAT_ID"].tolist() == [64236, 64236, 47406]
        assert found["NORAD_CAT_ID"].dtype.kind == "i"
        assert found["OBJECT_NAME"].tolist() == [
            "STARLINK-11691", "STARLINK-11691 [DTC]", "STARLINK-2133",
        ]
        assert found.loc[0, "LAUNCH_DATE"] == "2025-06-03"
        assert pd.isna(found.loc[1, "LAUNCH_DATE"])
        assert found.loc[2, "DECAY_DATE"] == "2026-01-30"

    def test_a_search_that_matched_nothing_is_cached_not_a_miss(self, tmp_path):
        cache = TextOrbitCache(tmp_path)
        cache.store_search("ZZZNOSUCHSAT", pd.DataFrame(columns=SEARCH_COLUMNS), fetched_at=FETCHED)
        snapshot = cache.get_search("ZZZNOSUCHSAT")
        assert snapshot is not None
        assert snapshot.found.empty
        assert list(snapshot.found.columns) == SEARCH_COLUMNS

    def test_an_absent_search_is_a_silent_miss(self, tmp_path):
        messages = []
        assert TextOrbitCache(tmp_path).get_search("NAVSTAR", log=messages.append) is None
        assert messages == []

    @pytest.mark.parametrize("other", ["navstar", "NAVSTAR ", " NAVSTAR", "NAVSTA_"])
    def test_the_query_as_sent_is_the_key(self, tmp_path, other):
        # Case, whitespace and wildcards all change what the service returns.
        cache = TextOrbitCache(tmp_path)
        cache.store_search("NAVSTAR", STARLINK, fetched_at=FETCHED)
        assert cache.search_path(other) != cache.search_path("NAVSTAR")
        assert cache.get_search(other) is None

    def test_a_new_search_replaces_the_old_one(self, tmp_path):
        cache = TextOrbitCache(tmp_path)
        cache.store_search("STARLINK", STARLINK, fetched_at=FETCHED)
        later = FETCHED + timedelta(days=3)
        cache.store_search("STARLINK", STARLINK.iloc[[2]], fetched_at=later)

        snapshot = cache.get_search("STARLINK")
        assert snapshot.found["NORAD_CAT_ID"].tolist() == [47406]
        assert snapshot.fetched_at == later

    def test_search_files_sit_beside_the_orbit_files_without_disturbing_them(self, tmp_path):
        cache = TextOrbitCache(tmp_path)
        cache.store(25544, make_catalogue_df([(25544, EPOCH)]))
        orbit_before = cache.path(25544).read_bytes()

        cache.store_search("ISS", _search_frame((25544, "ISS (ZARYA)", "1998-11-20", None)))

        assert cache.search_path("ISS").parent == cache.path(25544).parent == tmp_path
        assert cache.search_path("ISS").name.startswith("search-")
        assert cache.path(25544).read_bytes() == orbit_before
        assert len(cache.get(25544)) == 1
        # A directory scan for replay files skips both kinds of cache file.
        assert read_legacy_tle_records(tmp_path).empty

    def test_another_service_is_cached_under_its_own_key(self, tmp_path, monkeypatch):
        # The client reads BASE_URL when it sends a request; the cache must read
        # it at the same moment, or a mirror's result lands on the default
        # service's file labelled as the default service's.
        from satchecker_client import client

        cache = TextOrbitCache(tmp_path)
        default_url = client.BASE_URL
        cache.store_search("STARLINK", STARLINK, fetched_at=FETCHED)
        default_path = cache.search_path("STARLINK")

        monkeypatch.setattr(client, "BASE_URL", "https://mirror.example.org/tools")
        assert cache.search_path("STARLINK") != default_path
        assert cache.get_search("STARLINK") is None
        cache.store_search("STARLINK", STARLINK.iloc[[0]], fetched_at=FETCHED)
        mirror = json.loads(cache.search_path("STARLINK").read_text())
        assert mirror["base_url"] == "https://mirror.example.org/tools"

        monkeypatch.setattr(client, "BASE_URL", default_url)
        assert len(cache.get_search("STARLINK").found) == 3

    def test_a_store_writes_one_service_s_file_and_label_however_the_url_moves(
        self, tmp_path, monkeypatch
    ):
        # Read once per call: a change partway through must not put one
        # service's label in the other's file.
        from satchecker_client import cache as cache_module
        from satchecker_client import client

        default_url = client.BASE_URL
        monkeypatch.setattr(client, "BASE_URL", default_url)
        cache = TextOrbitCache(tmp_path)
        default_path = cache.search_path("STARLINK")
        monkeypatch.setattr(client, "BASE_URL", "https://mirror.example.org/tools")
        mirror_path = cache.search_path("STARLINK")
        monkeypatch.setattr(client, "BASE_URL", default_url)

        convert = cache_module._search_records_for_json

        def switch_then_convert(frame):
            client.BASE_URL = "https://mirror.example.org/tools"
            return convert(frame)

        monkeypatch.setattr(cache_module, "_search_records_for_json", switch_then_convert)
        cache.store_search("STARLINK", STARLINK, fetched_at=FETCHED)

        assert default_path.exists() and not mirror_path.exists()
        assert json.loads(default_path.read_text())["base_url"] == default_url

    def test_a_read_checks_the_file_against_the_service_it_opened(self, tmp_path, monkeypatch):
        # Switch the URL after the file is opened and before it is checked, so a
        # check that rereads it would compare against the other service.
        from satchecker_client import cache as cache_module
        from satchecker_client import client

        monkeypatch.setattr(client, "BASE_URL", client.BASE_URL)
        cache = TextOrbitCache(tmp_path)
        cache.store_search("STARLINK", STARLINK, fetched_at=FETCHED)
        load = cache_module.json.load

        def load_then_switch(handle):
            envelope = load(handle)
            client.BASE_URL = "https://mirror.example.org/tools"
            return envelope

        monkeypatch.setattr(cache_module.json, "load", load_then_switch)
        messages = []
        assert cache.get_search("STARLINK", log=messages.append) is not None
        assert messages == []

    def test_missing_values_are_written_as_null_not_nan(self, tmp_path):
        # pandas 3 reads a missing string back as NaN, and json.dump writes NaN as
        # a bare token no strict JSON reader accepts. Put one in explicitly, so
        # this holds under pandas 2 as well.
        found = STARLINK.astype({"LAUNCH_DATE": object})
        found.loc[1, "LAUNCH_DATE"] = float("nan")
        cache = TextOrbitCache(tmp_path)
        cache.store_search("STARLINK", found, fetched_at=FETCHED)
        text = cache.search_path("STARLINK").read_text()
        assert "NaN" not in text
        envelope = json.loads(text)
        assert envelope["records"][1]["LAUNCH_DATE"] is None
        assert envelope["count"] == 3

    def test_a_naive_fetched_at_is_taken_as_utc(self, tmp_path):
        cache = TextOrbitCache(tmp_path)
        cache.store_search("STARLINK", STARLINK, fetched_at=FETCHED.replace(tzinfo=None))
        assert cache.get_search("STARLINK").fetched_at == FETCHED

    @pytest.mark.parametrize(
        "corrupt, reason",
        [
            (lambda path, env: path.write_text("{not json"), "invalid JSON"),
            (lambda path, env: _rewrite(path, env, schema_version=99), "schema_version"),
            (lambda path, env: _rewrite(path, env, endpoint="get-nearest-tle"), "endpoint"),
            (lambda path, env: _rewrite(path, env, base_url="https://example.org/tools"), "service"),
            (lambda path, env: _rewrite(path, env, name="NAVSTAR"), "is for 'NAVSTAR'"),
            (lambda path, env: _rewrite(path, env, fetched_at="yesterday"), "fetched_at"),
            (lambda path, env: _rewrite(path, env, count=2), "count"),
            (lambda path, env: _rewrite_row(path, env, 0, LAUNCH_DATE="2025-6-03"), "YYYY-MM-DD"),
            (lambda path, env: _rewrite_row(path, env, 0, NORAD_CAT_ID=64236.5), "non-integer"),
            (lambda path, env: _rewrite_row(path, env, 0, OBJECT_NAME=None), "no OBJECT_NAME"),
            (lambda path, env: _drop_key(path, env, 0, "RCS_SIZE"), "search columns"),
            (lambda path, env: path.write_text(json.dumps([env])), "not an object"),
            (lambda path, env: _rewrite(path, env, records={"0": env["records"][0]}), "not a list"),
        ],
    )
    def test_an_unusable_search_file_is_reported_and_treated_as_a_miss(
        self, tmp_path, corrupt, reason
    ):
        cache = TextOrbitCache(tmp_path)
        cache.store_search("STARLINK", STARLINK, fetched_at=FETCHED)
        path = cache.search_path("STARLINK")
        corrupt(path, json.loads(path.read_text()))

        messages = []
        assert cache.get_search("STARLINK", log=messages.append) is None
        assert len(messages) == 1
        assert "unusable" in messages[0]
        if reason != "invalid JSON":
            assert reason in messages[0]

    @pytest.mark.parametrize(
        "bad",
        [
            STARLINK.drop(columns=["DECAY_DATE"]),
            STARLINK.assign(LAUNCH_DATE="2025/06/03"),
            STARLINK.assign(OBJECT_NAME=None),
            STARLINK.assign(NORAD_CAT_ID=0),
            STARLINK.assign(NORAD_CAT_ID=None),
            STARLINK.assign(NORAD_CAT_ID="sixty-four"),
            STARLINK.assign(OBJECT_TYPE=5),
            pd.DataFrame(columns=["NORAD_CAT_ID", "OBJECT_NAME"]),
        ],
        ids=[
            "missing column", "bad date", "null name", "non-positive id", "null id",
            "non-numeric id", "non-string field", "empty result missing columns",
        ],
    )
    def test_an_invalid_result_is_refused_and_the_old_file_kept(self, tmp_path, bad):
        cache = TextOrbitCache(tmp_path)
        cache.store_search("STARLINK", STARLINK, fetched_at=FETCHED)
        before = cache.search_path("STARLINK").read_bytes()
        with pytest.raises(CacheValidationError):
            cache.store_search("STARLINK", bad)
        assert cache.search_path("STARLINK").read_bytes() == before

    @pytest.mark.parametrize("name, error", [("", ValueError), ("   ", ValueError), (None, TypeError)])
    def test_an_empty_or_non_string_name_is_refused(self, tmp_path, name, error):
        cache = TextOrbitCache(tmp_path)
        with pytest.raises(error):
            cache.search_path(name)
        with pytest.raises(error):
            cache.get_search(name)
        with pytest.raises(error):
            cache.store_search(name, STARLINK)


def _rewrite(path, envelope, **changes):
    envelope.update(changes)
    path.write_text(json.dumps(envelope))


def _rewrite_row(path, envelope, index, **changes):
    envelope["records"][index].update(changes)
    path.write_text(json.dumps(envelope))


def _drop_key(path, envelope, index, key):
    del envelope["records"][index][key]
    path.write_text(json.dumps(envelope))

