"""Freezing a run's orbital inputs, and reading them back exactly.

A replay exists to make one thing reproducible: the records a previous run
actually propagated. Every shortcut that is ordinarily reasonable is therefore a
defect here — rounding a float to ten decimal places, skipping a row that will
not read, taking the first of two records for one satellite, asking the cache
for the one that is missing. Each of those silently changes the orbital input of
a run whose whole purpose is to keep it fixed.

Two files, written together and read as a pair:

``used_orbits.json``
    An explicit orbit table, in the ``{column: {index: value}}`` shape
    :func:`~satchecker_client.cache.read_orbit_file` and the forgiving directory
    scan both read. Not a managed cache envelope: this is the caller's file, not
    a shared one, and it holds exactly what was used rather than everything
    learned.
``norad_ids.yaml``
    The satellites, one per line, in the order they were used. Despite the
    extension it is read as the first column of a text file — the grammar it has
    always had — so no YAML parser is involved and none is a dependency.

What each record carries is what reads back as *itself*: a TLE's two lines,
since every element is encoded in them, plus the checksum provenance nothing can
re-derive; an OMM's epoch and its seven elements, since nothing else carries
them. Derived values are not written — ``EPOCH_JD`` and ``SEMIMAJOR_AXIS`` are
recomputed on every read, and a second copy on disk is one a later edit can
silently contradict.

Numbers are written through the standard library's JSON encoder, which writes a
float as ``repr`` does: the shortest text that reads back as the same double.
``DataFrame.to_json`` does not — at its default precision it rounds an element
outright, and even at its maximum it writes 0.0066635 as 0.006663499999999999,
a different double and so a different trajectory.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import cache as cache_module
from .cache import CacheValidationError
from .records import (
    CHECKSUM_STATUS_FIELD,
    KIND_FIELD,
    KIND_OMM,
    KIND_TLE,
    OMM_ELEMENT_COLUMNS,
    record_kind,
    validated_record,
)
from .resolve import OrbitInputError, _checked_norad_id, _missing


__all__ = [
    "REPLAY_IDS_FILE",
    "REPLAY_RECORDS_FILE",
    "load_replay_orbits",
    "save_orbits_for_reuse",
    "save_replay_orbits",
]


#: The two files a completed run writes, and the only two a frozen replay reads.
REPLAY_IDS_FILE = "norad_ids.yaml"
REPLAY_RECORDS_FILE = "used_orbits.json"

#: What each kind needs written out to read back as itself. Both keep whatever
#: provider and fetch provenance the record arrived with.
_REPLAY_COLUMNS = {
    KIND_TLE: (
        KIND_FIELD,
        "OBJECT_NAME",
        "TLE_LINE1",
        "TLE_LINE2",
        CHECKSUM_STATUS_FIELD,
        "DATA_SOURCE",
        "FETCHED_AT",
    ),
    KIND_OMM: (
        KIND_FIELD,
        "OBJECT_NAME",
        "OBJECT_ID",
        "EPOCH",
        *OMM_ELEMENT_COLUMNS,
        "DATA_SOURCE",
        "FETCHED_AT",
    ),
}

#: Of those, the ones without which the file cannot be read back as the record
#: it claims to be. A cell missing here is an invalid record; one missing
#: anywhere else in :data:`_REPLAY_COLUMNS` is one kind's column on the other
#: kind's row, which a mixed table has by construction.
_REPLAY_REQUIRED = {
    KIND_TLE: ("TLE_LINE1", "TLE_LINE2"),
    KIND_OMM: ("EPOCH", *OMM_ELEMENT_COLUMNS),
}


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

def _own_norad_id(record):
    """The record's own ``NORAD_CAT_ID``, checked, or ``None`` if it has none.

    Validated rather than cast, and by the new layer's check rather than the
    primitive's alone. ``int(25544.5)`` is 25544, so a lossy repair here lets a
    record whose identity disagrees with the ID it is filed against pass the
    alignment check and be written as the satellite it is not; and a NumPy
    boolean, which the primitive is documented as unable to refuse, is a
    perfectly ordinary ``1``. Raises ``ValueError`` for an identity that is
    present and unusable; only a record with no identity at all returns
    ``None``.
    """
    value = record.get("NORAD_CAT_ID")
    if _missing(value):
        return None
    return _checked_norad_id(value, "orbit record")


def _replay_record(norad_id: int, record: dict) -> dict:
    """One record validated, canonicalised and projected onto the replay columns.

    Validation comes first, because the projection cannot tell a missing cell
    from an invalid one: it drops every null, which is right for the OMM columns
    a TLE row acquires in a mixed frame and wrong for an OMM's own mean motion.
    A dropped element writes a file that reads back as a record nothing can
    propagate — a replay that cannot replay.

    Validated permissively on purpose, and there is no policy parameter here.
    The checksum decision was made when the record was accepted, and
    :func:`~satchecker_client.records.validated_record` never *upgrades* a
    status, so this writes the provenance the record already carries and
    launders nothing: the reader applies its own policy to it.
    """
    try:
        record = validated_record(record, allow_missing_checksum=True)
    except (ValueError, TypeError) as error:
        raise ValueError(
            f"the record filed against NORAD {norad_id} cannot be written to a "
            f"replay file: {error}"
        ) from error
    kind = record_kind(record)
    row = {"NORAD_CAT_ID": int(norad_id), KIND_FIELD: kind}
    for column in _REPLAY_COLUMNS[kind]:
        value = record.get(column)
        if _missing(value):
            if column in _REPLAY_REQUIRED[kind]:
                raise ValueError(
                    f"the {kind.upper()} record for NORAD {norad_id} has no "
                    f"{column}, which a replay of it needs"
                )
            continue
        row[column] = value
    return row


def _json_scalar(value):
    """A JSON-encodable copy of one cell, preserving a double exactly.

    A NumPy scalar is unwrapped with ``.item()``, which yields the Python float
    :func:`json.dumps` then writes through ``repr``. A missing value becomes
    ``null`` so a mixed TLE/OMM table stays standard JSON, which a bare ``NaN``
    would not be. An infinity is refused outright: no orbital element may be
    one, and the ``Infinity`` literal is not JSON either. So is anything that is
    not a scalar at all — a mapping or a list encodes perfectly well and reads
    back as a cell no line or element can be read from.
    """
    if _missing(value):
        return None
    item = getattr(value, "item", None)
    if item is not None and getattr(value, "shape", ()) == ():
        value = item()
    if isinstance(value, float) and value in (float("inf"), float("-inf")):
        raise ValueError(
            f"an orbit record carries {value!r}, which is not a number a record "
            "may hold and not valid JSON"
        )
    if not isinstance(value, (str, int, float)):
        raise TypeError(
            f"an orbit record carries a {type(value).__name__}, which is not a "
            "value one of its cells may hold"
        )
    return value


def _projected(norad_ids, records) -> list:
    """Aligned IDs and records, each validated against the other, as saved rows.

    ``zip`` would truncate to the shorter of the two and write a file that reads
    back cleanly while describing different satellites than the run used, so the
    alignment is checked rather than assumed. Neither sequence is truth-tested:
    a NumPy array raises on that rather than answering "is it empty", which
    would turn a satellite-free run into a crash.
    """
    ids = (
        []
        if norad_ids is None
        else [
            _checked_norad_id(norad_id, f"norad_ids[{position}]")
            for position, norad_id in enumerate(norad_ids)
        ]
    )
    rows = [] if records is None else list(records)
    if len(ids) != len(rows):
        raise ValueError(
            f"norad_ids and records must be aligned sequences, got {len(ids)} "
            f"ID(s) and {len(rows)} record(s). Truncating to the shorter one "
            "would write a replay file describing different satellites than the "
            "run used."
        )
    projected = []
    for norad_id, record in zip(ids, rows):
        record = record.to_dict() if hasattr(record, "to_dict") else dict(record)
        try:
            own = _own_norad_id(record)
        except ValueError as error:
            raise ValueError(
                f"the record filed against NORAD {norad_id} has an unusable "
                f"identity: {error}. Repairing it would save a record of "
                "whichever satellite the repair happened to name."
            ) from error
        if own is not None and own != norad_id:
            raise ValueError(
                f"the record filed against NORAD {norad_id} carries "
                f"NORAD_CAT_ID {own}; the IDs and the records a run saves must "
                "be aligned or the replay reproduces the wrong satellites"
            )
        projected.append(_replay_record(norad_id, record))
    return projected


def _table(projected: list) -> str:
    """The saved rows as one column-oriented JSON table, serialised.

    Serialised here, before anything is opened, so a record that cannot be
    written never leaves a file half replaced. ``allow_nan`` is the final guard:
    the non-standard ``NaN`` and ``Infinity`` literals are not values an orbit
    record may hold, and :func:`_json_scalar` has already refused the ones it
    can name.
    """
    columns: list = []
    for row in projected:
        columns += [column for column in row if column not in columns]
    payload = {
        column: {
            str(index): _json_scalar(row.get(column))
            for index, row in enumerate(projected)
        }
        for column in columns
    }
    return json.dumps(payload, allow_nan=False)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def save_orbits_for_reuse(path, norad_ids, records) -> str:
    """Write the records a run used to *path*, as an explicit orbit table.

    ``norad_ids`` and ``records`` are aligned sequences — an
    :class:`~satchecker_client.resolve.OrbitResolution`'s
    :meth:`~satchecker_client.resolve.OrbitResolution.norad_ids` and
    :meth:`~satchecker_client.resolve.OrbitResolution.records` produce them —
    and every supplied ID is validated rather than cast, as each record's own
    identity is: a truncated ID agrees with whichever satellite the truncation
    happened to name.

    Several rows for one satellite are allowed. This is a table of what a run
    used, and a run may legitimately propagate a satellite more than once;
    :func:`save_replay_orbits` is the stricter pair, where one saved ID must
    match exactly one record.

    ``RECORD_KIND`` is written explicitly. Inference exists for exports we did
    not write; for a file written here there is no reason to make a later reader
    guess. Always writes, and returns the path, an empty selection included:
    writing nothing would make "this run used no satellites" and "this is not a
    saved run" the same state on disk.
    """
    text = _table(_projected(norad_ids, records))
    path = str(path)
    with open(path, "w") as handle:
        handle.write(text)
    return path


def save_replay_orbits(directory, norad_ids, records) -> tuple:
    """Write the two files a frozen replay reads, and return their paths.

    ``(ids_path, records_path)``, in that order. The IDs must be unique here,
    unlike :func:`save_orbits_for_reuse`: the pair is read back by matching one
    saved record to each saved ID, and a satellite listed twice makes that
    matching a choice, which is the reselection a replay exists to prevent.

    Both files are validated and serialised before either destination is opened,
    so nothing that fails validation leaves a pair half written. They are then
    written one after the other, which is not a transaction: a failure between
    them raises, and the directory is left with what had been written.
    """
    projected = _projected(norad_ids, records)
    saved_ids = [row["NORAD_CAT_ID"] for row in projected]
    repeated = sorted({nid for nid in saved_ids if saved_ids.count(nid) > 1})
    if repeated:
        raise ValueError(
            f"a frozen replay needs exactly one record per saved satellite, but "
            f"NORAD {repeated} appear(s) more than once. Save this selection "
            "with save_orbits_for_reuse if a satellite is meant to be used "
            "twice."
        )
    table = _table(projected)
    ids_text = "".join(f"{norad_id}\n" for norad_id in saved_ids)

    directory = Path(directory)
    ids_path = directory / REPLAY_IDS_FILE
    records_path = directory / REPLAY_RECORDS_FILE
    ids_path.write_text(ids_text)
    records_path.write_text(table)
    return str(ids_path), str(records_path)


# ---------------------------------------------------------------------------
# Reading a pair back
# ---------------------------------------------------------------------------

def _read_replay_ids(path: Path) -> list:
    """The saved IDs, in saved order, with duplicates refused.

    The first column of a text file: ``#`` comments and blank lines are
    ignored, the first whitespace-separated token of every other line is the ID
    and anything after it — a satellite name, a note — is not read. Unlike an
    ordinary ID list this does not de-duplicate: two lines naming one satellite
    means the file disagrees with itself and cannot be matched one to one
    against the saved records.
    """
    try:
        text = path.read_text()
    except (OSError, UnicodeError) as error:
        # Decoding is part of reading the file: bytes that are not text are an
        # unreadable ID list, not a line this can name a number for, and either
        # way the caller gets the path and the reason rather than a bare
        # UnicodeDecodeError from inside the reader.
        raise OrbitInputError(
            f"frozen orbit replay could not read {path}: {error}. A replay reads "
            f"only {REPLAY_IDS_FILE} and {REPLAY_RECORDS_FILE} from the "
            "directory it is given, and has nothing else to fall back to.",
            path=path,
        ) from error

    saved: list = []
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            norad_id = _checked_norad_id(line.split()[0], f"{path}:{line_number}")
        except ValueError as error:
            raise OrbitInputError(
                f"{path} line {line_number} is not a NORAD catalogue ID: {error}",
                path=path,
                row=line_number,
            ) from error
        if norad_id in saved:
            raise OrbitInputError(
                f"{path} lists NORAD {norad_id} more than once; a frozen replay "
                "needs exactly one saved record per saved satellite, so the ID "
                "list has to be unique",
                path=path,
                row=line_number,
                norad_id=norad_id,
            )
        saved.append(norad_id)
    return saved


def load_replay_orbits(directory, *, allow_missing_checksum) -> tuple:
    """The saved IDs and records of a previous run, frozen exactly as saved.

    Reads only :data:`REPLAY_IDS_FILE` and :data:`REPLAY_RECORDS_FILE` from
    *directory* and returns ``(norad_ids, records)`` — the saved IDs in saved
    order, and one aligned record each. An empty saved selection is ``([], [])``:
    a completed run that used no satellites.

    **There is no second source.** No directory scan, no name search, no managed
    cache, no request, and no age-based reselection: a replay uses its saved
    records however far their epochs are from the observation, because that is
    what makes them the same records. Anything short of exact therefore stops,
    naming the file and the satellite — skipping a record, taking the first of
    two, or asking the cache would each silently change the orbital input.

    *allow_missing_checksum* is required, because there is no safe default for
    it. It applies to provenance as well as to lines: a record saved as
    ``unverified_missing_checksum`` needs it on every pass, whatever its lines
    carry now, so a permissive run cannot be laundered into a strict one by
    saving it.
    """
    directory = Path(directory)
    ids_path = directory / REPLAY_IDS_FILE
    records_path = directory / REPLAY_RECORDS_FILE

    norad_ids = _read_replay_ids(ids_path)
    try:
        frame = cache_module.read_orbit_file(records_path)
    except (CacheValidationError, OSError, ValueError) as error:
        raise OrbitInputError(
            f"frozen orbit replay could not read {records_path}: {error}. A "
            "replay has no alternative source by design, so it stops here "
            "rather than resolving these satellites from somewhere else.",
            path=records_path,
        ) from error

    rows_by_id: dict = {}
    for row_number, row in enumerate(frame.to_dict(orient="records")):
        try:
            norad_id = _own_norad_id(row)
        except ValueError as error:
            raise OrbitInputError(
                f"row {row_number} of {records_path} is not filed against a "
                f"satellite: {error}",
                path=records_path,
                row=row_number,
            ) from error
        if norad_id is None:
            raise OrbitInputError(
                f"row {row_number} of {records_path} has no NORAD_CAT_ID, so it "
                "cannot be matched to a saved satellite",
                path=records_path,
                row=row_number,
            )
        rows_by_id.setdefault(norad_id, []).append((row_number, row))

    unlisted = sorted(set(rows_by_id) - set(norad_ids))
    if unlisted:
        raise OrbitInputError(
            f"{records_path} carries record(s) for NORAD {unlisted}, which "
            f"{ids_path} does not list. The two files describe one selection and "
            "have to agree about it.",
            path=records_path,
        )

    records: list = []
    for norad_id in norad_ids:
        saved = rows_by_id.get(norad_id, [])
        if not saved:
            raise OrbitInputError(
                f"{ids_path} lists NORAD {norad_id} but {records_path} holds no "
                "record for it. A replay cannot fetch the missing one — that "
                "would make it a different run — so it stops here.",
                path=records_path,
                norad_id=norad_id,
            )
        if len(saved) > 1:
            raise OrbitInputError(
                f"{records_path} holds {len(saved)} records for NORAD "
                f"{norad_id}; a frozen replay needs exactly one, since choosing "
                "between them would be reselecting the orbital input it exists "
                "to freeze.",
                path=records_path,
                norad_id=norad_id,
            )
        row_number, row = saved[0]
        try:
            records.append(
                validated_record(row, allow_missing_checksum=allow_missing_checksum)
            )
        except (ValueError, TypeError) as error:
            raise OrbitInputError(
                f"the saved record for NORAD {norad_id} in {records_path} is not "
                f"acceptable under this run's policy: {error}. A run that "
                "accepted TLE lines without their checksum digits has to say so "
                "again to replay them: allow_missing_checksum=True.",
                path=records_path,
                row=row_number,
                norad_id=norad_id,
            ) from error
    return norad_ids, records
