"""Which satellites a catalogue search leaves in play at a given epoch.

:func:`~satchecker_client.client.search_satellites` reports catalogue entries as
SatChecker serves them, and an entry is not a satellite: one NORAD ID can span
several rows, one per name, and those rows need not carry the same dates. This
module answers the question most callers of a name search actually have — which
of the matched satellites could have been in orbit at the epoch they are
modelling — using only what the catalogue states, and ruling out nothing it does
not.

That makes it a shortlist, not a verdict. The catalogue's dates are incomplete,
and some are true of an object without answering the question: debris is listed
with its parent's launch date, so fragments of a 2009 collision are candidates
in 2008. And one object can be listed under two NORAD IDs, both of which survive
here. Whether a candidate existed at the epoch is settled by fetching its record
near that epoch and checking the record's age — which the caller must do anyway,
since neither record endpoint reports that it has nothing near the epoch asked
for. A former analyst number whose records stop months earlier fails that check;
so does debris asked about before it was created.
"""

from __future__ import annotations

import math
from datetime import datetime

import pandas as pd

from ._time import jd_to_datetime

#: Columns :func:`in_orbit_candidates` returns, one row per NORAD ID.
CANDIDATE_COLUMNS = ["NORAD_CAT_ID", "OBJECT_NAME", "LAUNCH_DATE", "DECAY_DATE"]


def _checked_date(value, column: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError) as e:
        raise ValueError(f"{column} values must be YYYY-MM-DD dates; got {value!r}") from e
    return value


def in_orbit_candidates(found: pd.DataFrame, epoch_jd: float) -> pd.DataFrame:
    """The satellites in *found* that the catalogue does not rule out at *epoch_jd*.

    *found* is a :func:`~satchecker_client.client.search_satellites` result. Its
    rows are combined per NORAD ID into one row with :data:`CANDIDATE_COLUMNS`,
    in the order the IDs first appear:

    - ``OBJECT_NAME`` — the first name listed for the satellite;
    - ``LAUNCH_DATE`` — the earliest launch date on any of its rows, or null;
    - ``DECAY_DATE`` — the latest decay date on any of its rows, or null.

    Taking the earliest launch and the latest decay keeps the widest window any
    row supports: a null on one alias row means that row does not say, not that
    the satellite never launched or never decayed.

    A satellite is dropped only when that window excludes the epoch: launched
    after the epoch's UTC date, or decayed before it. Both comparisons are by
    whole day and keep the epoch's own day, since the catalogue does not say what
    time of day either event happened. A satellite with no known date is kept.

    See the module documentation for why a candidate is not yet a satellite
    known to be in orbit, and what settles that.
    """
    required = CANDIDATE_COLUMNS
    missing = [column for column in required if column not in found.columns]
    if missing:
        raise ValueError(
            f"found is missing columns {missing}; pass a search_satellites result"
        )
    epoch = float(epoch_jd)
    if not math.isfinite(epoch):
        raise ValueError(f"epoch_jd must be finite; got {epoch_jd!r}")
    day = jd_to_datetime(epoch).date().isoformat()

    # name, earliest launch, latest decay; dicts keep first-appearance order.
    combined: dict[int, list] = {}
    for norad_id, name, launch, decay in zip(
        found["NORAD_CAT_ID"],
        found["OBJECT_NAME"],
        found["LAUNCH_DATE"],
        found["DECAY_DATE"],
    ):
        entry = combined.setdefault(int(norad_id), [name, None, None])
        if not pd.isna(launch):
            launch = _checked_date(launch, "LAUNCH_DATE")
            if entry[1] is None or launch < entry[1]:
                entry[1] = launch
        if not pd.isna(decay):
            decay = _checked_date(decay, "DECAY_DATE")
            if entry[2] is None or decay > entry[2]:
                entry[2] = decay

    rows = [
        (norad_id, name, launch, decay)
        for norad_id, (name, launch, decay) in combined.items()
        if (launch is None or launch <= day) and (decay is None or decay >= day)
    ]
    frame = pd.DataFrame(rows, columns=CANDIDATE_COLUMNS)
    frame["NORAD_CAT_ID"] = frame["NORAD_CAT_ID"].astype(int)
    return frame
