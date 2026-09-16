"""Kind dispatch: what a record is, when it is usable, and what it means.

These are the tests that let the policy layer stay format-blind. If
``record_kind`` mis-identifies a record, or the two kinds disagree about the
elements they describe for one satellite, everything above them is resolving
the wrong orbit with no visible error — so the equivalence between kinds is
asserted directly rather than inferred from higher-level behaviour.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest

from satchecker_client.records import (
    KIND_OMM,
    KIND_TLE,
    RecordKindError,
    norad_id_of,
    record_elements,
    record_epoch_jd,
    record_kind,
    validate_record,
)
from satchecker_client._time import datetime_to_jd, jd_to_datetime

from .tle_helpers import (  # noqa: F401  block_network is an autouse fixture
    STRAY_BACKSLASH_PAIR,
    block_network,
    both_kinds,
    jd,
    make_omm,
    make_record,
    make_tle,
    make_tle_record,
)


_EPOCH = jd(2026, 8, 1)


# ---------------------------------------------------------------------------
# Kind
# ---------------------------------------------------------------------------

class TestRecordKind:

    @both_kinds
    def test_explicit_kind_is_used(self, kind):
        assert record_kind(make_record(kind, 25544, _EPOCH)) == kind

    def test_tle_is_inferred_from_its_lines(self):
        record = make_tle_record(25544, _EPOCH)
        del record["RECORD_KIND"]
        assert record_kind(record) == KIND_TLE

    def test_omm_is_inferred_from_its_element_columns(self):
        record = make_omm(25544, _EPOCH)
        del record["RECORD_KIND"]
        assert record_kind(record) == KIND_OMM

    def test_spacetrack_gp_json_without_a_kind_field_resolves(self):
        # docs/orbits.md promises a Space-Track `gp`/`gp_history` export can be
        # dropped into extra_orbit_dir unconverted. That JSON carries TLE lines
        # and element columns but no kind field, and the lines are what we
        # validate against, so it must infer as a TLE rather than an OMM.
        record = make_tle_record(25544, _EPOCH)
        del record["RECORD_KIND"]
        record.update(
            {k: v for k, v in make_omm(25544, _EPOCH).items() if k != "RECORD_KIND"}
        )
        assert record_kind(record) == KIND_TLE
        assert validate_record(record) == 25544

    def test_unknown_kind_is_rejected_rather_than_guessed(self):
        record = make_omm(25544, _EPOCH, RECORD_KIND="ephemeris")
        with pytest.raises(RecordKindError, match="unknown RECORD_KIND"):
            record_kind(record)

    def test_a_record_that_is_neither_is_rejected(self):
        with pytest.raises(RecordKindError, match="neither TLE lines nor"):
            record_kind({"NORAD_CAT_ID": 25544, "OBJECT_NAME": "ISS"})

    def test_a_partial_omm_element_set_is_not_an_omm(self):
        record = make_omm(25544, _EPOCH)
        del record["RECORD_KIND"]
        del record["MEAN_MOTION"]
        with pytest.raises(RecordKindError):
            record_kind(record)

    @pytest.mark.parametrize("empty", [None, float("nan"), "", "   "])
    def test_a_present_but_empty_line_does_not_make_it_a_tle(self, empty):
        # A frame that has been concatenated with TLE rows carries the line
        # columns on every row, filled with nulls. Treating those as a TLE would
        # send an OMM record into the line parser.
        record = make_omm(25544, _EPOCH)
        del record["RECORD_KIND"]
        record["TLE_LINE1"] = empty
        record["TLE_LINE2"] = empty
        assert record_kind(record) == KIND_OMM


# ---------------------------------------------------------------------------
# Epoch
# ---------------------------------------------------------------------------

class TestRecordEpoch:

    @both_kinds
    def test_epoch_round_trips(self, kind):
        # The TLE line field is quantised to 1e-8 day (~0.9 ms); OMM is exact.
        record = make_record(kind, 25544, _EPOCH)
        assert record_epoch_jd(record) == pytest.approx(_EPOCH, abs=1e-7)

    def test_the_two_kinds_agree_on_one_satellites_epoch(self):
        tle = record_epoch_jd(make_tle_record(25544, _EPOCH))
        omm = record_epoch_jd(make_omm(25544, _EPOCH))
        assert abs(tle - omm) < 1e-7

    @pytest.mark.parametrize(
        "text",
        [
            "2026-08-01T00:00:00",
            "2026-08-01T00:00:00Z",
            "2026-08-01T00:00:00z",
            "2026-08-01T00:00:00+00:00",
            "2026-08-01T02:00:00+02:00",
        ],
        ids=["naive", "zulu", "lowercase-zulu", "offset", "shifted-offset"],
    )
    def test_iso8601_spellings_all_reach_the_same_instant(self, text):
        record = make_omm(25544, _EPOCH, EPOCH=text)
        assert record_epoch_jd(record) == pytest.approx(jd(2026, 8, 1))

    @pytest.mark.parametrize(
        "bad", ["", "   ", "not a date", "2026-13-01T00:00:00", 12345.6]
    )
    def test_unparseable_omm_epochs_are_rejected(self, bad):
        with pytest.raises(ValueError):
            record_epoch_jd(make_omm(25544, _EPOCH, EPOCH=bad))

    def test_a_missing_omm_epoch_is_rejected(self):
        record = make_omm(25544, _EPOCH)
        del record["EPOCH"]
        with pytest.raises(ValueError, match="missing EPOCH"):
            record_epoch_jd(record)

    def test_a_pre_sputnik_epoch_is_rejected(self):
        record = make_omm(25544, _EPOCH, EPOCH="1901-01-01T00:00:00")
        with pytest.raises(ValueError, match="plausible window"):
            record_epoch_jd(record)

    def test_an_epoch_years_in_the_future_is_rejected(self):
        far = datetime.now(timezone.utc) + timedelta(days=800)
        record = make_omm(25544, _EPOCH, EPOCH=far.replace(tzinfo=None).isoformat())
        with pytest.raises(ValueError, match="plausible window"):
            record_epoch_jd(record)

    def test_a_slightly_future_epoch_is_accepted(self):
        # Element sets are legitimately published a little ahead of their epoch.
        soon = datetime.now(timezone.utc) + timedelta(days=7)
        record = make_omm(25544, _EPOCH, EPOCH=soon.replace(tzinfo=None).isoformat())
        assert record_epoch_jd(record) == pytest.approx(datetime_to_jd(soon), abs=1e-6)

    def test_a_tle_epoch_is_re_derived_and_not_read_from_the_epoch_column(self):
        # The EPOCH column is a provider field; line 1 is the authority. A record
        # whose two disagree must follow the lines.
        record = make_tle_record(25544, _EPOCH, EPOCH="1999-01-01T00:00:00")
        assert record_epoch_jd(record) == pytest.approx(_EPOCH, abs=1e-7)


# ---------------------------------------------------------------------------
# Elements
# ---------------------------------------------------------------------------

class TestRecordElements:

    def test_both_kinds_describe_the_same_orbit(self):
        tle = record_elements(make_tle_record(25544, _EPOCH))
        omm = record_elements(make_omm(25544, _EPOCH))
        assert set(tle) == set(omm)
        for column in tle:
            if column == "EPOCH_JD":
                assert abs(tle[column] - omm[column]) < 1e-7
            else:
                assert tle[column] == pytest.approx(omm[column])

    @both_kinds
    def test_column_order_is_identical_across_kinds(self, kind):
        # _add_parsed_elements builds a DataFrame from these dicts, so a
        # kind-dependent key order would produce kind-dependent column order.
        assert list(record_elements(make_record(kind, 25544, _EPOCH))) == [
            "INCLINATION",
            "RA_OF_ASC_NODE",
            "ECCENTRICITY",
            "ARG_OF_PERICENTER",
            "MEAN_ANOMALY",
            "MEAN_MOTION",
            "BSTAR",
            "SEMIMAJOR_AXIS",
            "EPOCH_JD",
        ]

    def test_a_providers_semimajor_axis_is_not_trusted(self):
        # OMM may carry SEMIMAJOR_AXIS; we recompute it from the mean motion so
        # the two kinds cannot disagree about a satellite they both describe.
        record = make_omm(25544, _EPOCH, SEMIMAJOR_AXIS=1.0)
        assert record_elements(record)["SEMIMAJOR_AXIS"] > 6000.0

    @pytest.mark.parametrize(
        "column,value",
        [
            ("INCLINATION", 181.0),
            ("INCLINATION", float("nan")),
            ("RA_OF_ASC_NODE", 360.0),
            ("ECCENTRICITY", 1.0),
            ("ARG_OF_PERICENTER", -1.0),
            ("MEAN_ANOMALY", float("inf")),
            ("MEAN_MOTION", 0.0),
            ("MEAN_MOTION", -1.0),
            ("BSTAR", float("nan")),
        ],
    )
    def test_out_of_range_omm_elements_are_rejected(self, column, value):
        # OMM has no checksum, so these bounds are the only defence against a
        # corrupted element that would otherwise parse cleanly.
        with pytest.raises(ValueError):
            record_elements(make_omm(25544, _EPOCH, **{column: value}))

    def test_an_unreadable_omm_element_is_rejected(self):
        with pytest.raises(ValueError):
            record_elements(make_omm(25544, _EPOCH, MEAN_MOTION="not a number"))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidateRecord:

    @both_kinds
    def test_a_good_record_returns_its_norad_id(self, kind):
        assert validate_record(make_record(kind, 25544, _EPOCH)) == 25544

    def test_a_tle_id_comes_from_the_lines_not_the_row(self):
        # This is the cross-check OMM cannot offer: a row filed under the wrong
        # satellite is caught because the lines carry their own identifier.
        record = make_tle_record(25544, _EPOCH, NORAD_CAT_ID=38833)
        assert validate_record(record) == 25544

    def test_a_corrupted_tle_checksum_is_rejected(self):
        line1, line2 = make_tle(25544, _EPOCH)
        record = make_tle_record(25544, _EPOCH, TLE_LINE1=line1[:68] + "9")
        with pytest.raises(ValueError, match="checksum"):
            validate_record(record)

    @pytest.mark.parametrize("bad", ["abc", float("nan"), float("inf"), 25544.5, None])
    def test_an_unusable_omm_norad_id_is_rejected(self, bad):
        with pytest.raises(ValueError):
            validate_record(make_omm(25544, _EPOCH, NORAD_CAT_ID=bad))

    def test_an_omm_with_a_bad_epoch_does_not_validate(self):
        # The epoch window is part of validation, not only of epoch derivation:
        # get-nearest-omm answers a pre-handover request with its earliest
        # record instead of reporting that it has none, so a wrong epoch is the
        # failure this format is prone to.
        record = make_omm(25544, _EPOCH, EPOCH="1899-01-01T00:00:00")
        with pytest.raises(ValueError, match="plausible window"):
            validate_record(record)

    def test_an_omm_id_check_is_vacuous_by_construction(self):
        # Documenting the asymmetry as a test: there is only one identifier in
        # an OMM record, so validate_record can only hand back what it was told.
        record = make_omm(25544, _EPOCH, NORAD_CAT_ID=38833)
        assert validate_record(record) == 38833

    def test_a_datetime_epoch_object_is_accepted(self):
        record = make_omm(25544, _EPOCH, EPOCH=jd_to_datetime(_EPOCH))
        assert record_epoch_jd(record) == pytest.approx(_EPOCH, abs=1e-6)


# ---------------------------------------------------------------------------
# Canonical single-record validation, with provenance
# ---------------------------------------------------------------------------

def _validated_record():
    """Import the helper under test at call time.

    It does not exist yet, so importing it at module level would stop this whole
    file from collecting. Hoist it into the ``satchecker_client.records`` import
    at the top once the helper lands.
    """
    from satchecker_client.records import validated_record

    return validated_record


def _without_checksum(line: str) -> str:
    """A valid line with its column-69 checksum digit removed.

    Derived from a checksum-valid line rather than written out, so a case can
    drop the digit from line 1 alone, line 2 alone, or both — which the
    archive's own checksum-less pairs cannot express.
    """
    return line[:68]


def _wrong_checksum(line: str) -> str:
    """The same line carrying the next digit up, so the checksum cannot match."""
    return line[:68] + str((int(line[68]) + 1) % 10)


class TestValidatedRecord:
    """One record in, one canonical copy out, with its assurance stated.

    :func:`validate_record` answers "is this usable?" and hands back an ID.
    This answers the question a consumer actually has before it propagates
    anything: *what exactly am I about to use* — the lines in standard form, the
    row and line identities agreeing, the metadata intact, and a checksum status
    that records what the record's assurance is rather than what this run's
    policy happened to allow.

    The status is provenance, not a re-derivation. A record accepted once
    without checksums must still read as unverified after a repair, a save and a
    replay, or a permissive run would launder it into every strict run after it.
    """

    def test_validated_record_returns_canonical_copy(self):
        validated_record = _validated_record()
        record = make_tle_record(
            26867,
            jd(2010, 5, 31),
            TLE_LINE1=STRAY_BACKSLASH_PAIR[0],
            TLE_LINE2=STRAY_BACKSLASH_PAIR[1],
            DATA_SOURCE="spacetrack",
            DATE_COLLECTED="2025-05-01 00:00:00 UTC",
        )
        original = dict(record)

        result = validated_record(record)

        # The backslash is gone and the checksum still verifies the line, so the
        # record is exactly as trustworthy as a clean one and says so.
        assert result["TLE_LINE1"] == STRAY_BACKSLASH_PAIR[0][:-1]
        assert result["TLE_LINE2"] == STRAY_BACKSLASH_PAIR[1]
        assert result["TLE_CHECKSUM_STATUS"] == "verified"
        assert result["RECORD_KIND"] == KIND_TLE
        assert result["NORAD_CAT_ID"] == 26867
        assert result["OBJECT_NAME"] == record["OBJECT_NAME"]
        assert result["DATA_SOURCE"] == "spacetrack"
        assert result["DATE_COLLECTED"] == "2025-05-01 00:00:00 UTC"
        # A copy: the caller's mapping is not rewritten under it.
        assert record == original

    def test_validated_record_accepts_a_dataframe_row(self):
        # Records arrive as rows of a fetched or cached frame, so a Series has to
        # be as acceptable as a mapping, and a plain dict has to come back.
        validated_record = _validated_record()
        frame = pd.DataFrame([make_tle_record(25544, _EPOCH)])

        result = validated_record(frame.loc[0])

        assert isinstance(result, dict)
        assert result["NORAD_CAT_ID"] == 25544
        assert result["TLE_CHECKSUM_STATUS"] == "verified"

    @pytest.mark.parametrize(
        "drop", [(1,), (2,), (1, 2)], ids=["line 1", "line 2", "both lines"]
    )
    def test_validated_record_missing_checksum_requires_opt_in(self, drop):
        validated_record = _validated_record()
        line1, line2 = make_tle(25544, _EPOCH)
        if 1 in drop:
            line1 = _without_checksum(line1)
        if 2 in drop:
            line2 = _without_checksum(line2)
        record = make_tle_record(25544, _EPOCH, TLE_LINE1=line1, TLE_LINE2=line2)

        with pytest.raises(ValueError, match="checksum"):
            validated_record(record)

        accepted = validated_record(record, allow_missing_checksum=True)

        # Accepted as they came: a recomputed digit would make an unverifiable
        # line look verified to the next reader.
        assert accepted["TLE_LINE1"] == line1
        assert accepted["TLE_LINE2"] == line2
        assert len(accepted["TLE_LINE1"]) == (68 if 1 in drop else 69)
        assert len(accepted["TLE_LINE2"]) == (68 if 2 in drop else 69)
        assert accepted["TLE_CHECKSUM_STATUS"] == "unverified_missing_checksum"

    def test_validated_record_provenance_cannot_upgrade_unverified_input(self):
        validated_record = _validated_record()

        # Checksum-less lines that claim to be verified. The claim is not
        # evidence; nothing in the record can verify those digits.
        line1, line2 = (_without_checksum(line) for line in make_tle(25544, _EPOCH))
        claimed = make_tle_record(
            25544,
            _EPOCH,
            TLE_LINE1=line1,
            TLE_LINE2=line2,
            TLE_CHECKSUM_STATUS="verified",
        )
        with pytest.raises(ValueError, match="checksum"):
            validated_record(claimed)
        assert (
            validated_record(claimed, allow_missing_checksum=True)["TLE_CHECKSUM_STATUS"]
            == "unverified_missing_checksum"
        )

        # Checksum-valid lines carrying an unverified provenance. The checksums
        # verify the lines as they stand; they say nothing about the source they
        # were reconstructed from, so the status stands and the opt-in is still
        # required.
        carried = make_tle_record(
            25544, _EPOCH, TLE_CHECKSUM_STATUS="unverified_missing_checksum"
        )
        with pytest.raises(ValueError, match="unverified"):
            validated_record(carried)
        assert (
            validated_record(carried, allow_missing_checksum=True)["TLE_CHECKSUM_STATUS"]
            == "unverified_missing_checksum"
        )

        # A status we do not recognise is a record we cannot classify.
        unknown = make_tle_record(25544, _EPOCH, TLE_CHECKSUM_STATUS="trusted")
        for allow in (False, True):
            with pytest.raises(ValueError, match="TLE_CHECKSUM_STATUS"):
                validated_record(unknown, allow_missing_checksum=allow)

    @pytest.mark.parametrize("allow", [False, True], ids=["strict", "permissive"])
    @pytest.mark.parametrize(
        "overrides, message",
        [
            # The lines carry their own identifier, so a record filed under
            # another satellite is catchable — and must be caught, because every
            # caller looks the record up by the row's ID.
            ({"NORAD_CAT_ID": 38833}, "38833"),
            ({"NORAD_CAT_ID": 25544.5}, "25544.5"),
            ({"NORAD_CAT_ID": 0}, "0"),
            ({"NORAD_CAT_ID": -25544}, "-25544"),
        ],
        ids=["another satellite", "fractional", "zero", "negative"],
    )
    def test_validated_record_checks_row_and_embedded_ids(
        self, allow, overrides, message
    ):
        validated_record = _validated_record()
        record = make_tle_record(25544, _EPOCH, **overrides)
        with pytest.raises(ValueError, match=message):
            validated_record(record, allow_missing_checksum=allow)

    @pytest.mark.parametrize("allow", [False, True], ids=["strict", "permissive"])
    def test_validated_record_rejects_a_corrupt_checksum_under_either_policy(self, allow):
        # Allowing *missing* checksums must not weaken what a present one means.
        validated_record = _validated_record()
        line1, _ = make_tle(25544, _EPOCH)
        record = make_tle_record(25544, _EPOCH, TLE_LINE1=_wrong_checksum(line1))
        with pytest.raises(ValueError, match="checksum"):
            validated_record(record, allow_missing_checksum=allow)

    def test_validated_record_preserves_omm_and_metadata(self):
        validated_record = _validated_record()
        record = make_omm(
            25544,
            _EPOCH,
            ECCENTRICITY=0.0066635,
            BSTAR=3.2e-05,
            DATA_SOURCE="spacetrack",
            DATE_COLLECTED="2026-08-13 03:34:14 UTC",
            FETCHED_AT="2026-09-16T12:30:05Z",
        )

        result = validated_record(record)

        assert result["RECORD_KIND"] == KIND_OMM
        # Exact doubles, not approximately equal ones: a replayed trajectory
        # built from a rounded element is a different trajectory.
        assert result["ECCENTRICITY"] == 0.0066635
        assert result["BSTAR"] == 3.2e-05
        assert result["EPOCH"] == record["EPOCH"]
        assert result["OBJECT_ID"] == record["OBJECT_ID"]
        assert result["DATA_SOURCE"] == "spacetrack"
        assert result["DATE_COLLECTED"] == "2026-08-13 03:34:14 UTC"
        assert result["FETCHED_AT"] == "2026-09-16T12:30:05Z"
        # OMM has no checksum and no second identifier, so there is no
        # verification to claim. Stamping one would make the two kinds look
        # equally checked when they are not.
        assert "TLE_CHECKSUM_STATUS" not in result

    @pytest.mark.parametrize("allow", [False, True], ids=["strict", "permissive"])
    @pytest.mark.parametrize(
        "overrides",
        [
            {"INCLINATION": 999.0},
            {"MEAN_MOTION": 0.0},
            {"BSTAR": float("nan")},
            {"EPOCH": "1899-01-01T00:00:00"},
            {"EPOCH": "not a date"},
        ],
        ids=["inclination", "mean motion", "non-finite bstar", "epoch window", "bad epoch"],
    )
    def test_validated_record_still_rejects_an_invalid_omm(self, allow, overrides):
        # The checksum opt-in is about TLE lines. It must not become a general
        # "accept anything" switch for the kind that has no checksum at all.
        validated_record = _validated_record()
        with pytest.raises(ValueError):
            validated_record(
                make_omm(25544, _EPOCH, **overrides), allow_missing_checksum=allow
            )


class TestNoradIdOf:
    """IDs must convert exactly and be positive integers."""

    def _record(self, value):
        record = make_omm(25544, _EPOCH)
        record["NORAD_CAT_ID"] = value
        return record

    @pytest.mark.parametrize("bad", [0, -1, "0", "-25544", 0.0, True, False])
    def test_zero_negative_and_boolean_ids_are_rejected(self, bad):
        with pytest.raises(ValueError):
            norad_id_of(self._record(bad))

    @pytest.mark.parametrize(
        "huge", [2**53 + 1, str(2**53 + 1), Decimal(2**53 + 1)]
    )
    def test_ids_above_float_precision_convert_exactly(self, huge):
        # A float round-trip would round this to 2**53 — a different satellite.
        assert norad_id_of(self._record(huge)) == 2**53 + 1

    def test_plain_string_and_integral_float_forms_still_convert(self):
        assert norad_id_of(self._record("25544")) == 25544
        assert norad_id_of(self._record(25544.0)) == 25544
