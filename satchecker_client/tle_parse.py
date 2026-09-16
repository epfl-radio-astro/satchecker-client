"""TLE line parsing shared by cache validation and element extraction.

One parser, two consumers: callers derive OMM-style orbital elements through it,
and :mod:`satchecker_client.cache` validates envelopes through it — so anything
the element parser consumes is, by construction, exactly what validation
exercises. Imports only the standard library and this
package's small Julian-date helper.

Satellite identifiers use the Alpha-5 scheme where needed: catalogue numbers
above 99999 encode the leading digits as a letter (``E8493`` -> 148493; the
letters I and O are excluded to avoid confusion with 1 and 0).
"""

from __future__ import annotations

import calendar
import math
from datetime import datetime, timedelta

from ._time import datetime_to_jd


_MU_KM3_S2 = 398600.4418  # Earth gravitational parameter, km^3/s^2

_ALPHA5_EXCLUDED = {"I", "O"}


def decode_norad_id(field: str) -> int:
    """Decode a 5-character TLE satellite field, including Alpha-5 identifiers.

    Plain digits decode directly (``"25544"`` -> 25544). Alpha-5 fields carry a
    leading letter worth 10-33 (A-H, J-N, P-Z; I and O are excluded), so
    ``"E8493"`` -> 148493 and ``"Z9999"`` -> 339999. Raises ``ValueError`` for
    anything else.
    """
    s = str(field).strip()
    if not s:
        raise ValueError("empty satellite identifier field")
    if s.isdigit():
        return int(s)
    head, tail = s[0], s[1:]
    if head.isalpha() and head.isupper() and head not in _ALPHA5_EXCLUDED and tail.isdigit():
        value = ord(head) - 55  # A -> 10
        if head > "I":
            value -= 1
        if head > "O":
            value -= 1
        return value * 10_000 + int(tail)
    raise ValueError(f"invalid satellite identifier field {field!r}")


def _parse_eccentricity_field(field: str) -> float:
    """Parse the eccentricity field (columns 27-33 of line 2, implied ``0.``).

    Exactly seven ASCII digits, enforced rather than assumed: the implied
    leading decimal point means a blank field would otherwise parse as
    ``float("0.") == 0.0`` — a checksum-valid line with its eccentricity
    *missing* silently becoming a circular orbit. (BSTAR is different: a blank
    exponential field genuinely means zero and is accepted as such.)
    """
    if not (len(field) == 7 and field.isascii() and field.isdigit()):
        raise ValueError(f"TLE eccentricity field must be 7 digits, got {field!r}")
    return float("0." + field)


def _parse_exp_field(field: str) -> float:
    """Parse a TLE exponential field (e.g. '-11606-4' -> -0.11606e-4)."""
    s = field.strip()
    if not s or s in ("+00000-0", "00000-0", "00000+0"):
        return 0.0
    sign = 1.0
    if s[0] in "+-":
        sign = -1.0 if s[0] == "-" else 1.0
        s = s[1:]
    mantissa = s[:-2].replace(" ", "")
    exponent = int(s[-2:])
    if not mantissa:
        return 0.0
    return sign * float("0." + mantissa) * (10.0 ** exponent)


def tle_epoch_jd(line1: str) -> float:
    """UTC Julian Date of a TLE epoch (line 1 columns 19-32).

    The day-of-year bound depends on the decoded year. A flat ``< 367`` test would
    let day 366 through for a non-leap year, where ``timedelta`` then rolls it
    silently into 1 January of the *following* year — a checksum-correct but
    impossible epoch, modelled a year late without any complaint.
    """
    epoch_year = int(line1[18:20])
    epoch_day = float(line1[20:32])
    year = 2000 + epoch_year if epoch_year < 57 else 1900 + epoch_year
    # Day-of-year d means "d - 1 days after 1 January", so the first instant of the
    # next year is d = days_in_year + 1 and the bound is strict.
    days_in_year = 366 if calendar.isleap(year) else 365
    if not 0.0 < epoch_day < days_in_year + 1.0:
        raise ValueError(
            f"TLE epoch day out of range for {year}: {epoch_day} "
            f"(must be > 0 and < {days_in_year + 1}; {year} has {days_in_year} days)"
        )
    dt = datetime(year, 1, 1) + timedelta(days=epoch_day - 1.0)
    return datetime_to_jd(dt)


#: OMM-style element columns every record kind must supply, paired with the
#: human-readable names the range errors use.
ELEMENT_FIELDS = (
    ("INCLINATION", "inclination"),
    ("RA_OF_ASC_NODE", "RAAN"),
    ("ECCENTRICITY", "eccentricity"),
    ("ARG_OF_PERICENTER", "argument of pericenter"),
    ("MEAN_ANOMALY", "mean anomaly"),
    ("MEAN_MOTION", "mean motion"),
    ("BSTAR", "BSTAR"),
)


def validate_elements(elements: dict, context: str = "orbital element set") -> None:
    """Check finiteness and physical range on a set of OMM-style elements.

    Shared by both record kinds. A TLE reaches it through
    :func:`parse_tle_elements` after the fields have been read out of the two
    lines; an OMM record reaches it with the provider's own numbers. That
    matters more for OMM than for TLE: OMM has no modulo-10 checksum and no
    second copy of the satellite identifier to cross-check, so these bounds are
    the *only* thing standing between a corrupted element and a silently wrong
    trajectory. See :mod:`satchecker_client.records` for what else is done to
    narrow that gap.

    *context* names the kind in the error text, so the messages a TLE produces
    are unchanged from when these checks lived inline. Raises ``ValueError``.
    """
    values = {}
    for column, name in ELEMENT_FIELDS:
        if column not in elements:
            raise ValueError(f"{context} is missing {column}")
        try:
            values[name] = float(elements[column])
        except (TypeError, ValueError) as e:
            raise ValueError(f"{context} has an unreadable {column}: {e}") from e

    non_finite = [name for name, value in values.items() if not math.isfinite(value)]
    if non_finite:
        raise ValueError(f"{context} has non-finite fields: {', '.join(non_finite)}")
    if not 0.0 <= values["inclination"] <= 180.0:
        raise ValueError(f"{context} inclination out of range: {values['inclination']}")
    for name in ("RAAN", "argument of pericenter", "mean anomaly"):
        if not 0.0 <= values[name] < 360.0:
            raise ValueError(f"{context} {name} out of range: {values[name]}")
    if not 0.0 <= values["eccentricity"] < 1.0:
        raise ValueError(f"{context} eccentricity out of range: {values['eccentricity']}")
    if values["mean motion"] <= 0.0:
        raise ValueError(
            f"{context} mean motion must be positive, got {values['mean motion']}"
        )


def semimajor_axis_km(mean_motion_rev_day: float) -> float:
    """Semi-major axis in km from a mean motion in rev/day, via Kepler's third law.

    Reproduces the value Space-Track's OMM reported, so downstream consumers see
    the same number whichever kind the elements came from. Raises
    ``ZeroDivisionError`` for a zero mean motion, which callers translate.
    """
    n_rad_s = float(mean_motion_rev_day) * 2.0 * math.pi / 86400.0
    return (_MU_KM3_S2 / n_rad_s ** 2) ** (1.0 / 3.0)


def parse_tle_elements(line1: str, line2: str) -> dict:
    """Derive OMM-style orbital elements from a TLE pair.

    Angles are in degrees, mean motion in rev/day and the semi-major axis in km
    — matching the units Space-Track's OMM reported, so downstream consumers are
    unchanged. ``SEMIMAJOR_AXIS`` is computed from the mean motion via Kepler's
    third law (reproduces the Space-Track OMM value). Raises ``ValueError`` (or
    ``ZeroDivisionError`` for a zero mean motion) on malformed fields.
    """
    elements = {
        "INCLINATION": float(line2[8:16]),
        "RA_OF_ASC_NODE": float(line2[17:25]),
        "ECCENTRICITY": _parse_eccentricity_field(line2[26:33]),
        "ARG_OF_PERICENTER": float(line2[34:42]),
        "MEAN_ANOMALY": float(line2[43:51]),
        "MEAN_MOTION": float(line2[52:63]),  # rev/day
        "BSTAR": _parse_exp_field(line1[53:61]),
    }
    validate_elements(elements, "TLE")
    elements["SEMIMAJOR_AXIS"] = semimajor_axis_km(elements["MEAN_MOTION"])
    elements["EPOCH_JD"] = tle_epoch_jd(line1)
    return elements


#: A TLE line is exactly 69 columns, the last of which is a modulo-10 checksum.
TLE_LINE_LENGTH = 69

#: Two defects of SatChecker's historical TLE archive, whose records backfilled
#: from Space-Track in May 2025 carry a backslash after the last column of line 1
#: and in some cases no checksum digit at all. :func:`validate_tle_line` always
#: accepts the first, which the checksum still verifies, and accepts the second
#: only when asked to.
STRAY_BACKSLASH = "stray trailing backslash"
MISSING_CHECKSUM = "missing checksum digit"

#: Columns (0-based) that hold a separator or a decimal point in every
#: well-formed line. Checked on a line with no checksum digit, where nothing else
#: would notice a character dropped from mid-line: that shifts every later field
#: one column left, and a shifted field can still parse. It cannot catch a
#: character dropped after the last anchor — line 2's mean motion digits and
#: revolution number, line 1's element set number — because only unanchored
#: digits move.
_LAYOUT = {
    1: {8: " ", 17: " ", 23: ".", 32: " ", 34: ".", 43: " ", 52: " ", 61: " ", 63: " "},
    2: {
        7: " ", 11: ".", 16: " ", 20: ".", 25: " ", 33: " ",
        37: ".", 42: " ", 46: ".", 51: " ", 54: ".",
    },
}


def tle_checksum(line: str) -> int:
    """Modulo-10 checksum over a TLE line's first 68 columns.

    Digits count as themselves and a minus sign counts as 1; everything else
    (letters, plus signs, decimal points, spaces) counts as zero.
    """
    return sum(
        int(c) if c.isdigit() else (1 if c == "-" else 0) for c in line[:68]
    ) % 10


def _without_stray_backslash(line: str) -> str:
    return line[:-1] if line.endswith("\\") else line


def tle_line_defects(line: str) -> tuple[str, ...]:
    """Which accepted defects *line* has: :data:`STRAY_BACKSLASH`, :data:`MISSING_CHECKSUM`.

    Empty for a standard line. It describes a line without judging it, so pair it
    with :func:`validate_tle_line`: a line of the wrong width reports no defect
    here and still fails there.
    """
    defects = []
    trimmed = line.rstrip()
    if trimmed.endswith("\\"):
        defects.append(STRAY_BACKSLASH)
    if len(_without_stray_backslash(trimmed)) == TLE_LINE_LENGTH - 1:
        defects.append(MISSING_CHECKSUM)
    return tuple(defects)


def validate_tle_line(
    line: str, number: int, *, allow_missing_checksum: bool = False
) -> str:
    """Validate one TLE line's marker, width and checksum; return it in standard form.

    The checksum is what makes single-character corruption detectable. Without
    it, a flipped digit inside a fixed-width numeric field parses cleanly, stays
    in range, and silently shifts the modelled trajectory — the failure mode that
    is hardest to notice downstream. Trailing whitespace and newlines are
    tolerated (files routinely carry them); leading layout is not, because every
    field is read by column.

    Two defects of SatChecker's historical TLE archive are handled, and the
    returned line has them undone as far as they can be:

    - A **backslash after the last column** is always removed. The checksum is
      then verified as usual, so a line accepted this way is exactly as
      trustworthy as a clean one.
    - A **68-column line**, its checksum digit missing, is rejected unless
      *allow_missing_checksum* is true, and then accepted only if every separator
      and decimal-point column is where the format puts it. Nothing verifies its
      digits: a flipped digit goes unnoticed, and so does a digit dropped from
      line 2's mean motion or revolution number, which shifts no anchored
      column. The mean motion of a genuine line and of one missing a fractional
      digit look equally valid. Accept such lines only where their source is
      known to omit checksums, and say so to whoever uses the result.

    :func:`tle_line_defects` reports which of these a line had.
    """
    if not isinstance(line, str):
        raise ValueError("TLE lines must be strings")
    trimmed = _without_stray_backslash(line.rstrip())
    if not trimmed.startswith(f"{number} "):
        raise ValueError(f"TLE line {number} must start with '{number} '")
    if len(trimmed) == TLE_LINE_LENGTH - 1:
        if not allow_missing_checksum:
            raise ValueError(
                f"TLE line {number} must be {TLE_LINE_LENGTH} characters, got "
                f"{len(trimmed)}: it has no checksum digit, and missing checksums "
                "were not allowed"
            )
        for column, expected in _LAYOUT[number].items():
            if trimmed[column] != expected:
                raise ValueError(
                    f"TLE line {number} has no checksum digit and column "
                    f"{column + 1} is {trimmed[column]!r}, not {expected!r}; a "
                    "character is missing from the line"
                )
        return trimmed
    if len(trimmed) != TLE_LINE_LENGTH:
        raise ValueError(
            f"TLE line {number} must be {TLE_LINE_LENGTH} characters, "
            f"got {len(trimmed)}"
        )
    check_digit = trimmed[68]
    if not check_digit.isdigit():
        raise ValueError(
            f"TLE line {number} checksum column is {check_digit!r}, not a digit"
        )
    expected = tle_checksum(trimmed)
    if int(check_digit) != expected:
        raise ValueError(
            f"TLE line {number} checksum mismatch: line carries {check_digit}, "
            f"content gives {expected}"
        )
    return trimmed


def validate_tle_pair(line1, line2, *, allow_missing_checksum: bool = False) -> int:
    """Fully validate a TLE pair; return its decoded NORAD catalogue ID.

    Checks each line's width and modulo-10 checksum, then runs the *same* parser
    downstream element extraction uses, so every consumed field (epoch,
    inclination, RAAN, eccentricity, argument of pericenter, mean anomaly, mean
    motion, BSTAR) must parse. Also decodes the satellite identifier embedded in
    both lines (Alpha-5 aware) and requires them to agree. Raises ``ValueError``
    on any problem, which callers treat as "reject this record and try another
    source".

    *allow_missing_checksum* is passed to :func:`validate_tle_line`; see there
    for what accepting a line without a checksum gives up.
    """
    if not (isinstance(line1, str) and isinstance(line2, str)):
        raise ValueError("TLE lines must be strings")
    line1 = validate_tle_line(line1, 1, allow_missing_checksum=allow_missing_checksum)
    line2 = validate_tle_line(line2, 2, allow_missing_checksum=allow_missing_checksum)
    id1 = decode_norad_id(line1[2:7])
    id2 = decode_norad_id(line2[2:7])
    if id1 != id2:
        raise ValueError(f"TLE line identifiers disagree: {id1} vs {id2}")
    try:
        parse_tle_elements(line1, line2)
    except ZeroDivisionError as e:
        raise ValueError("TLE mean motion is zero") from e
    return id1
