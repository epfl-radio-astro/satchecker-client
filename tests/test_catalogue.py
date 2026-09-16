"""Offline tests for narrowing a catalogue search to one epoch."""

import pandas as pd
import pytest

from satchecker_client.catalogue import CANDIDATE_COLUMNS, in_orbit_candidates
from satchecker_client.client import SEARCH_COLUMNS

from .tle_helpers import block_network, jd  # noqa: F401


EPOCH = jd(2022, 8, 13)  # a UTC date of 2022-08-13


def _found(*rows):
    """A search_satellites-shaped frame; each row (id, name, launch, decay)."""
    return pd.DataFrame(
        [
            {
                "NORAD_CAT_ID": norad_id,
                "OBJECT_NAME": name,
                "OBJECT_ID": None,
                "OBJECT_TYPE": None,
                "RCS_SIZE": None,
                "LAUNCH_DATE": launch,
                "DECAY_DATE": decay,
            }
            for norad_id, name, launch, decay in rows
        ],
        columns=SEARCH_COLUMNS,
    )


def _ids(frame):
    return frame["NORAD_CAT_ID"].tolist()


def test_a_satellite_that_decayed_later_is_still_a_candidate():
    # The case a name search at a past epoch exists for: dropping everything
    # that has *ever* decayed would lose it.
    found = _found((53437, "STARLINK-4529", "2022-08-10", "2022-08-17"))
    assert _ids(in_orbit_candidates(found, EPOCH)) == [53437]


@pytest.mark.parametrize(
    "launch, decay, kept",
    [
        ("2022-08-14", None, False),  # launched after the epoch's day
        ("2022-08-13", None, True),  # launched on it
        (None, "2022-08-12", False),  # decayed before it
        (None, "2022-08-13", True),  # decayed on it
        ("2019-01-01", "2030-01-01", True),
        (None, None, True),  # nothing known rules nothing out
    ],
)
def test_only_the_catalogue_dates_rule_a_satellite_out(launch, decay, kept):
    found = _found((1, "SAT", launch, decay))
    assert _ids(in_orbit_candidates(found, EPOCH)) == ([1] if kept else [])


def test_alias_rows_combine_into_the_widest_window():
    # STARLINK-11691 [DTC] carries no dates; its null must not read as "never
    # launched" or "never decayed", nor displace the dates the other row has.
    found = _found(
        (64236, "STARLINK-11691", "2022-06-03", None),
        (64236, "STARLINK-11691 [DTC]", None, None),
        (70000, "SAT A", None, "2022-08-01"),
        (70000, "SAT A ALIAS", None, "2022-09-01"),
        (70001, "SAT B", "2022-09-01", None),
        (70001, "SAT B ALIAS", "2022-08-01", None),
    )
    frame = in_orbit_candidates(found, EPOCH)

    assert list(frame.columns) == CANDIDATE_COLUMNS
    assert _ids(frame) == [64236, 70000, 70001]
    by_id = frame.set_index("NORAD_CAT_ID")
    assert by_id.loc[64236, "OBJECT_NAME"] == "STARLINK-11691"
    assert by_id.loc[64236, "LAUNCH_DATE"] == "2022-06-03"
    assert pd.isna(by_id.loc[64236, "DECAY_DATE"])
    assert by_id.loc[70000, "DECAY_DATE"] == "2022-09-01"  # latest decay
    assert by_id.loc[70001, "LAUNCH_DATE"] == "2022-08-01"  # earliest launch


def test_one_object_under_two_ids_keeps_both():
    # Telling them apart needs records at the epoch, which this does not fetch.
    found = _found(
        (72115, "ONEWEB-0702", None, None),
        (61608, "ONEWEB-0702", "2024-10-20", None),
    )
    assert _ids(in_orbit_candidates(found, jd(2025, 1, 1))) == [72115, 61608]


def test_the_epoch_day_is_utc():
    found = _found((1, "SAT", None, "2022-08-13"))
    late_on_the_day = EPOCH + 23.5 / 24
    assert _ids(in_orbit_candidates(found, late_on_the_day)) == [1]
    assert _ids(in_orbit_candidates(found, EPOCH + 1)) == []


def test_ids_are_integers_in_first_appearance_order():
    found = _found((3, "C", None, None), (1, "A", None, None), (3, "C2", None, None))
    frame = in_orbit_candidates(found, EPOCH)
    assert _ids(frame) == [3, 1]
    assert frame["NORAD_CAT_ID"].dtype.kind == "i"


def test_an_empty_search_is_an_empty_frame_with_the_columns():
    frame = in_orbit_candidates(pd.DataFrame(columns=SEARCH_COLUMNS), EPOCH)
    assert frame.empty
    assert list(frame.columns) == CANDIDATE_COLUMNS


def test_a_frame_that_is_not_a_search_result_is_refused():
    with pytest.raises(ValueError, match="search_satellites result"):
        in_orbit_candidates(pd.DataFrame({"NORAD_CAT_ID": [1]}), EPOCH)


@pytest.mark.parametrize("column", ["LAUNCH_DATE", "DECAY_DATE"])
@pytest.mark.parametrize(
    "value",
    [
        "13/08/2022",
        # Unpadded: a real date, but "2022-8-01" sorts after "2022-08-13" and
        # would drop a satellite launched twelve days before the epoch.
        "2022-8-01",
        "2022-02-30",
        "2022-0A-01",
        "\uff12\uff10\uff12\uff12-08-01",  # full-width digits read by strptime
    ],
)
def test_an_unreadable_date_is_refused(column, value):
    found = _found((1, "SAT", None, None))
    found[column] = found[column].astype(object)
    found.loc[0, column] = value
    with pytest.raises(ValueError, match=f"{column} values must be YYYY-MM-DD"):
        in_orbit_candidates(found, EPOCH)


@pytest.mark.parametrize("epoch", [float("nan"), float("inf")])
def test_a_non_finite_epoch_is_refused(epoch):
    with pytest.raises(ValueError, match="finite"):
        in_orbit_candidates(_found((1, "SAT", None, None)), epoch)


def test_a_fractional_norad_id_is_refused_not_truncated():
    found = _found((25544.5, "SAT", None, None))
    with pytest.raises(ValueError, match="whole numbers"):
        in_orbit_candidates(found, EPOCH)

