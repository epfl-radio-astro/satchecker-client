"""Tests for the shared TLE parser (:mod:`satchecker_client.tle_parse`).

This module is the single parser used by both cache validation and orbital
element extraction, so these tests pin the identifier decoding (including
Alpha-5) and the guarantee that validation and parsing agree.
"""

import pytest

from satchecker_client.tle_parse import (
    ELEMENT_FIELDS,
    MISSING_CHECKSUM,
    STRAY_BACKSLASH,
    decode_norad_id,
    parse_tle_elements,
    semimajor_axis_km,
    tle_checksum,
    tle_line_defects,
    validate_elements,
    validate_tle_line,
    validate_tle_pair,
)

from .tle_helpers import (
    BACKSLASH_FOR_CHECKSUM_PAIR,
    NO_CHECKSUM_PAIR,
    STRAY_BACKSLASH_PAIR,
    jd,
    make_omm,
    make_tle,
    with_checksum,
)

_EPOCH = jd(2023, 2, 21, 13)
# Letters used by Alpha-5, in value order: I and O are excluded to avoid
# confusion with 1 and 0, so A=10 ... H=17, J=18 ... N=22, P=23 ... Z=33.
_ALPHA5_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"


class TestDecodeNoradId:

    @pytest.mark.parametrize(
        "field,expected",
        [("25544", 25544), ("00001", 1), ("99999", 99999),
         ("A0000", 100000), ("E8493", 148493), ("Z9999", 339999)],
    )
    def test_known_identifiers(self, field, expected):
        assert decode_norad_id(field) == expected

    def test_every_alpha5_letter_maps_to_its_spec_value(self):
        for i, ch in enumerate(_ALPHA5_LETTERS):
            assert decode_norad_id(f"{ch}1234") == (10 + i) * 10_000 + 1234

    @pytest.mark.parametrize("field", ["I1234", "O1234", "!1234", "e8493", "", "   "])
    def test_invalid_identifiers_raise(self, field):
        with pytest.raises(ValueError):
            decode_norad_id(field)


class TestValidateTlePair:

    def test_valid_pair_returns_decoded_id(self):
        assert validate_tle_pair(*make_tle(25544, _EPOCH)) == 25544

    def test_alpha5_pair_is_valid(self):
        l1, l2 = make_tle(25544, _EPOCH)
        assert validate_tle_pair(
            with_checksum("1 E8493" + l1[7:]), with_checksum("2 E8493" + l2[7:])
        ) == 148493

    def test_disagreeing_identifiers_raise(self):
        l1, _ = make_tle(25544, _EPOCH)
        _, l2 = make_tle(38833, _EPOCH)
        with pytest.raises(ValueError, match="identifiers disagree"):
            validate_tle_pair(l1, l2)

    @pytest.mark.parametrize("line", ["", "not a tle", 12345, None])
    def test_malformed_lines_raise(self, line):
        l1, l2 = make_tle(25544, _EPOCH)
        with pytest.raises(ValueError):
            validate_tle_pair(line, l2)

    @pytest.mark.parametrize(
        "start,stop",
        [(8, 16), (17, 25), (26, 33), (34, 42), (43, 51), (52, 63)],
        ids=["inclination", "raan", "eccentricity", "arg_pericenter",
             "mean_anomaly", "mean_motion"],
    )
    def test_validation_covers_every_field_the_parser_reads(self, start, stop):
        # Whatever parse_tle_elements consumes, validate_tle_pair must reject
        # when malformed — the two can never drift apart.
        l1, l2 = make_tle(25544, _EPOCH)
        bad2 = l2[:start] + "A" * (stop - start) + l2[stop:]
        with pytest.raises(ValueError):
            validate_tle_pair(l1, bad2)
        with pytest.raises(ValueError):
            parse_tle_elements(l1, bad2)

    def test_malformed_bstar_rejected(self):
        l1, l2 = make_tle(25544, _EPOCH)
        with pytest.raises(ValueError):
            validate_tle_pair(with_checksum(l1[:53] + "ABCDEFGH" + l1[61:]), l2)

    def test_out_of_range_epoch_day_rejected(self):
        l1, l2 = make_tle(25544, _EPOCH)
        with pytest.raises(ValueError, match="epoch day out of range"):
            validate_tle_pair(with_checksum(l1[:20] + "999.00000000" + l1[32:]), l2)

    @pytest.mark.parametrize(
        "start,stop,value",
        [(8, 16, "nan"), (17, 25, "inf"), (52, 63, "inf")],
        ids=["inclination_nan", "raan_inf", "mean_motion_inf"],
    )
    def test_non_finite_elements_are_rejected(self, start, stop, value):
        # Checksum-corrected, so this proves the *element* validation rejects it
        # rather than the line integrity check getting there first.
        l1, l2 = make_tle(25544, _EPOCH)
        bad2 = with_checksum(l2[:start] + value.rjust(stop - start) + l2[stop:])
        with pytest.raises(ValueError, match="non-finite"):
            validate_tle_pair(l1, bad2)

    @pytest.mark.parametrize("mean_motion", [" 0.00000000", "-1.00000000"])
    def test_non_positive_mean_motion_is_rejected(self, mean_motion):
        l1, l2 = make_tle(25544, _EPOCH)
        bad2 = with_checksum(l2[:52] + mean_motion + l2[63:])
        with pytest.raises(ValueError, match="must be positive"):
            validate_tle_pair(l1, bad2)


# ---------------------------------------------------------------------------
# Line width and checksum
# ---------------------------------------------------------------------------

class TestLineIntegrity:
    """The checksum is what makes single-character corruption detectable.

    Without it a flipped digit inside a fixed-width numeric field parses cleanly,
    stays in range, and silently shifts the modelled trajectory.
    """

    def _pair(self):
        return make_tle(25544, _EPOCH)

    def test_valid_pair_is_accepted(self):
        l1, l2 = self._pair()
        assert validate_tle_pair(l1, l2) == 25544

    def test_trailing_whitespace_is_tolerated(self):
        # Files routinely carry it; it is not part of the fixed-width columns.
        l1, l2 = self._pair()
        assert validate_tle_pair(l1 + "  \n", l2 + "\r\n") == 25544

    @pytest.mark.parametrize("line", [1, 2])
    def test_a_line_short_by_more_than_its_checksum_is_rejected(self, line):
        # Short by exactly the checksum digit is an accepted archive defect; see
        # TestArchiveDefects.
        pair = list(self._pair())
        pair[line - 1] = pair[line - 1][:-2]
        with pytest.raises(ValueError, match="69 characters"):
            validate_tle_pair(*pair)

    @pytest.mark.parametrize("line", [1, 2])
    def test_long_line_is_rejected(self, line):
        pair = list(self._pair())
        pair[line - 1] = pair[line - 1] + "0"
        with pytest.raises(ValueError, match="69 characters"):
            validate_tle_pair(*pair)

    @pytest.mark.parametrize("line", [1, 2])
    def test_wrong_checksum_is_rejected(self, line):
        pair = list(self._pair())
        bad = str((int(pair[line - 1][68]) + 1) % 10)
        pair[line - 1] = pair[line - 1][:68] + bad
        with pytest.raises(ValueError, match="checksum mismatch"):
            validate_tle_pair(*pair)

    @pytest.mark.parametrize("line", [1, 2])
    def test_non_digit_checksum_column_is_rejected(self, line):
        pair = list(self._pair())
        pair[line - 1] = pair[line - 1][:68] + "X"
        with pytest.raises(ValueError, match="checksum column"):
            validate_tle_pair(*pair)

    def test_single_character_corruption_in_a_numeric_field_is_caught(self):
        # The motivating case: an inclination digit flipped in transit. The value
        # still parses and stays in range, so only the checksum can catch it.
        l1, l2 = self._pair()
        corrupted = l2[:9] + ("6" if l2[9] != "6" else "7") + l2[10:]
        assert parse_tle_elements(l1, corrupted)["INCLINATION"] != \
            parse_tle_elements(l1, l2)["INCLINATION"]      # it would have changed the orbit
        with pytest.raises(ValueError, match="checksum mismatch"):
            validate_tle_pair(l1, corrupted)

    def test_checksum_counts_minus_signs_as_one(self):
        # The rule that distinguishes a TLE checksum from a plain digit sum.
        assert tle_checksum("-" * 68) == 68 % 10
        assert tle_checksum("1" * 68) == 68 % 10
        assert tle_checksum(" " * 68) == 0



class TestArchiveDefects:
    """The two defects of SatChecker's historical TLE archive that are accepted.

    The fixtures, in ``tle_helpers``, are pairs exactly as SatChecker served
    them.
    """

    STRAY_BACKSLASH_PAIR = STRAY_BACKSLASH_PAIR
    BACKSLASH_FOR_CHECKSUM_PAIR = BACKSLASH_FOR_CHECKSUM_PAIR
    NO_CHECKSUM_PAIR = NO_CHECKSUM_PAIR

    def test_a_stray_backslash_is_removed_and_the_checksum_still_verified(self):
        line1, line2 = self.STRAY_BACKSLASH_PAIR
        assert validate_tle_pair(line1, line2) == 26867
        assert validate_tle_line(line1, 1) == line1[:-1]
        assert len(validate_tle_line(line1, 1)) == 69
        assert tle_line_defects(line1) == (STRAY_BACKSLASH,)
        assert tle_line_defects(line2) == ()

    def test_a_backslash_in_place_of_the_checksum_leaves_an_unverified_line(self):
        line1, line2 = self.BACKSLASH_FOR_CHECKSUM_PAIR
        assert validate_tle_pair(line1, line2) == 25544
        assert validate_tle_line(line1, 1) == line1[:-1]
        assert len(validate_tle_line(line1, 1)) == 68
        assert tle_line_defects(line1) == (STRAY_BACKSLASH, MISSING_CHECKSUM)
        assert tle_line_defects(line2) == (MISSING_CHECKSUM,)

    def test_lines_without_checksums_parse_to_their_own_fields(self):
        # The fields are all in their columns, so the elements come out right:
        # these are the values printed in the lines themselves.
        elements = parse_tle_elements(*self.NO_CHECKSUM_PAIR)
        assert elements["INCLINATION"] == 51.6421
        assert elements["RA_OF_ASC_NODE"] == 205.3341
        assert elements["ECCENTRICITY"] == 0.000532
        assert elements["MEAN_MOTION"] == 15.54011654
        assert elements["BSTAR"] == pytest.approx(0.25686e-4)
        assert validate_tle_pair(*self.NO_CHECKSUM_PAIR) == 25544

    @pytest.mark.parametrize("line", [1, 2])
    def test_a_character_missing_mid_line_is_caught_without_a_checksum(self, line):
        # Drop one character from mid-line and one from the end, and the line is
        # 68 columns like an honest checksum-less one, with every later field
        # shifted. Only the layout check stands between that and a wrong orbit.
        pair = list(self.NO_CHECKSUM_PAIR)
        shifted = pair[line - 1][:20] + pair[line - 1][21:] + "0"
        pair[line - 1] = shifted
        assert len(shifted) == 68
        with pytest.raises(ValueError, match="a character is missing"):
            validate_tle_pair(*pair)

    def test_a_clean_line_less_its_checksum_digit_is_accepted(self):
        line1, line2 = make_tle(25544, _EPOCH)
        assert validate_tle_pair(line1[:68], line2) == 25544
        assert tle_line_defects(line1[:68]) == (MISSING_CHECKSUM,)

    def test_a_stray_backslash_does_not_excuse_a_wrong_checksum(self):
        line1, line2 = self.STRAY_BACKSLASH_PAIR
        wrong = line1[:68] + str((int(line1[68]) + 1) % 10) + "\\"
        with pytest.raises(ValueError, match="checksum mismatch"):
            validate_tle_pair(wrong, line2)

    def test_only_one_trailing_backslash_is_stray(self):
        line1, line2 = self.STRAY_BACKSLASH_PAIR
        with pytest.raises(ValueError, match="69 characters"):
            validate_tle_pair(line1 + "\\", line2)

    def test_clean_lines_report_no_defects(self):
        line1, line2 = make_tle(25544, _EPOCH)
        assert tle_line_defects(line1) == tle_line_defects(line2) == ()


# ---------------------------------------------------------------------------
# Shared element validation
# ---------------------------------------------------------------------------

class TestValidateElements:
    """The range and finiteness checks both record kinds go through.

    These used to live inline in :func:`parse_tle_elements`, where a TLE's
    checksum was the primary defence and they were a second line. For OMM they
    are the *only* line — there is no checksum and no second copy of the
    satellite identifier — so what they cover matters much more than it did.
    """

    def _elements(self):
        l1, l2 = make_tle(25544, _EPOCH)
        return {column: parse_tle_elements(l1, l2)[column] for column, _ in ELEMENT_FIELDS}

    def test_a_real_element_set_passes(self):
        validate_elements(self._elements())

    def test_an_omm_records_elements_pass(self):
        record = make_omm(25544, _EPOCH)
        validate_elements({column: record[column] for column, _ in ELEMENT_FIELDS})

    @pytest.mark.parametrize("column,_name", ELEMENT_FIELDS)
    def test_every_element_must_be_present(self, column, _name):
        elements = self._elements()
        del elements[column]
        with pytest.raises(ValueError, match=f"missing {column}"):
            validate_elements(elements)

    @pytest.mark.parametrize("column,_name", ELEMENT_FIELDS)
    def test_every_element_must_be_finite(self, column, _name):
        elements = self._elements()
        elements[column] = float("nan")
        with pytest.raises(ValueError, match="non-finite"):
            validate_elements(elements)

    @pytest.mark.parametrize(
        "column,value",
        [
            ("INCLINATION", -0.1),
            ("INCLINATION", 180.1),
            ("RA_OF_ASC_NODE", 360.0),
            ("ARG_OF_PERICENTER", -0.1),
            ("MEAN_ANOMALY", 360.0),
            ("ECCENTRICITY", 1.0),
            ("ECCENTRICITY", -0.1),
            ("MEAN_MOTION", 0.0),
        ],
    )
    def test_out_of_range_values_are_rejected(self, column, value):
        elements = self._elements()
        elements[column] = value
        with pytest.raises(ValueError):
            validate_elements(elements)

    def test_the_context_names_the_kind_in_the_message(self):
        elements = self._elements()
        elements["INCLINATION"] = 999.0
        with pytest.raises(ValueError, match="^OMM inclination out of range"):
            validate_elements(elements, "OMM")
        with pytest.raises(ValueError, match="^TLE inclination out of range"):
            validate_elements(elements, "TLE")

    def test_semimajor_axis_matches_the_parsers_own_value(self):
        l1, l2 = make_tle(25544, _EPOCH)
        parsed = parse_tle_elements(l1, l2)
        assert semimajor_axis_km(parsed["MEAN_MOTION"]) == parsed["SEMIMAJOR_AXIS"]


class TestEccentricityField:
    """The implied decimal point means a blank field would read as ``0.0``."""

    def _with_field(self, field):
        l1, l2 = make_tle(25544, _EPOCH)
        return l1, with_checksum(l2[:26] + field + l2[33:68])

    def test_a_blank_field_is_rejected_not_read_as_a_circular_orbit(self):
        l1, l2 = self._with_field(" " * 7)
        with pytest.raises(ValueError, match="eccentricity"):
            parse_tle_elements(l1, l2)
        with pytest.raises(ValueError, match="eccentricity"):
            validate_tle_pair(l1, l2)

    def test_internal_spaces_are_rejected(self):
        l1, l2 = self._with_field("12 4567")
        with pytest.raises(ValueError, match="eccentricity"):
            parse_tle_elements(l1, l2)

    def test_a_fully_digit_field_still_parses(self):
        l1, l2 = self._with_field("0006703")
        assert parse_tle_elements(l1, l2)["ECCENTRICITY"] == pytest.approx(0.0006703)
