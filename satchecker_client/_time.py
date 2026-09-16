"""Small UTC Julian-date conversions used internally by this package.

Deliberately hand-rolled rather than taken from Skyfield or Astropy: they are
two dozen lines, and carrying them keeps the dependency footprint to pandas.
"""

from datetime import datetime, timedelta, timezone


DAY_SECONDS = 86_400.0
UNIX_EPOCH = datetime(1970, 1, 1)
UNIX_EPOCH_JD = 2440587.5


def datetime_to_jd(value: datetime) -> float:
    """Convert a naive/UTC datetime to a UTC Julian Date."""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return UNIX_EPOCH_JD + (value - UNIX_EPOCH).total_seconds() / DAY_SECONDS


def jd_to_datetime(value: float) -> datetime:
    """Convert a UTC Julian Date to a naive UTC datetime."""
    return UNIX_EPOCH + timedelta(days=float(value) - UNIX_EPOCH_JD)


def is_iso_date(value) -> bool:
    """Whether *value* is a real calendar date written exactly as ``YYYY-MM-DD``.

    Exactly, because these dates are compared as strings: ``strptime`` alone also
    accepts ``2022-8-01``, which then sorts after ``2022-08-13``.
    """
    if not isinstance(value, str) or len(value) != 10 or value[4] != "-" or value[7] != "-":
        return False
    digits = value[:4] + value[5:7] + value[8:]
    # isascii too: isdigit accepts full-width and other Unicode digits, which
    # strptime also reads, and which sort differently from ASCII ones.
    if not (digits.isascii() and digits.isdigit()):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True

