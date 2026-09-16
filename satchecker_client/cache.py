"""Validated cache for SatChecker orbit records and catalogue searches.

Each satellite has one small, atomically-written JSON file containing every
validated record learned for it. A resolver can reuse one record for multiple
nearby observation epochs by comparing its epoch with the configurable
cache-reuse age. There are no catalogue buckets, snapshots, or settling states.

A single satellite's file may hold both record kinds at once, and around the
2026-07-12 archive handover it usually will: the last TLEs SatChecker ever
published for it, and the OMM element sets that follow. Which one a given
observation gets is decided by epoch distance in the caller's policy layer, not
here.
Everything below that has to be kind-aware is: what columns a record must have,
what makes two records duplicates, and what "valid" means.

Catalogue searches live in the same directory, one ``search-<key>.json`` file per
query, beside the ``orbit-<NORAD>.json`` files. They differ from orbit records in
the one way that matters for reuse: a record never changes once published, but a
search result does, as satellites launch, decay dates are added and alias rows
appear. So a search file holds one complete result and the time it was fetched,
and is replaced rather than merged; whether a result is still fresh enough is
the caller's decision.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from glob import glob
from pathlib import Path
from typing import Callable, Optional

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

import pandas as pd

from ._time import is_iso_date
from . import client
from .client import SEARCH_COLUMNS
from .tle_parse import MISSING_CHECKSUM, tle_line_defects, validate_tle_line
from .records import (
    KIND_FIELD,
    KIND_OMM,
    KIND_TLE,
    OMM_ELEMENT_COLUMNS,
    record_kind,
    validate_record,
)


#: Bumped from 1 when records stopped being TLE-only. The bump costs nothing:
#: :meth:`TextOrbitCache.get` already treats an unusable file as a warned cache
#: miss, so v1 files self-evict and are re-fetched with a clear log line instead
#: of needing a migration path.
SCHEMA_VERSION = 2

#: Version of the ``search-<key>.json`` envelope, independent of
#: :data:`SCHEMA_VERSION`: the two file kinds change for different reasons.
SEARCH_SCHEMA_VERSION = 1

#: The endpoint a search file caches; part of its key and checked on read.
_SEARCH_ENDPOINT = "search-satellites"

_FETCHED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class SearchSnapshot:
    """A cached catalogue search: the query, its complete result, and when it ran.

    ``found`` is what :func:`~satchecker_client.client.search_satellites`
    returned for ``name``, with :data:`~satchecker_client.client.SEARCH_COLUMNS`;
    an empty frame is a cached search that matched nothing, not a miss.
    ``fetched_at`` is a timezone-aware UTC datetime.
    """

    name: str
    found: pd.DataFrame
    fetched_at: datetime

#: What a record of each kind must carry to be worth validating at all.
REQUIRED_COLUMNS_BY_KIND = {
    KIND_TLE: ("NORAD_CAT_ID", "TLE_LINE1", "TLE_LINE2"),
    KIND_OMM: ("NORAD_CAT_ID", "EPOCH", *OMM_ELEMENT_COLUMNS),
}

#: What makes two records for one satellite the same record. A TLE is identified
#: by its lines, which encode the epoch; an OMM has no lines, so its epoch is the
#: identifying field.
DEDUPE_COLUMNS_BY_KIND = {
    KIND_TLE: ("NORAD_CAT_ID", "TLE_LINE1", "TLE_LINE2", "DATA_SOURCE"),
    KIND_OMM: ("NORAD_CAT_ID", "EPOCH", "DATA_SOURCE"),
}

#: The TLE column set, under its historical name.
REQUIRED_COLUMNS = REQUIRED_COLUMNS_BY_KIND[KIND_TLE]


class CacheValidationError(ValueError):
    """A per-satellite cache file is structurally or semantically invalid."""


def _validated_ids(frame: pd.DataFrame, expected_norad_id: int) -> pd.Series:
    """Satellite IDs from a cache frame, checked and belonging to one satellite."""
    if "NORAD_CAT_ID" not in frame.columns:
        raise CacheValidationError("orbit cache has no NORAD_CAT_ID column")
    if frame["NORAD_CAT_ID"].isnull().any():
        raise CacheValidationError("orbit cache has null values in NORAD_CAT_ID")
    try:
        ids = pd.to_numeric(frame["NORAD_CAT_ID"])
    except (TypeError, ValueError) as error:
        raise CacheValidationError(f"NORAD_CAT_ID is not numeric: {error}") from error
    if any(not math.isfinite(float(value)) or float(value) != round(float(value)) for value in ids):
        raise CacheValidationError("NORAD_CAT_ID contains non-finite or non-integer values")
    ids = ids.astype(int)
    if (ids <= 0).any():
        raise CacheValidationError("NORAD_CAT_ID contains non-positive values")
    if set(ids) != {int(expected_norad_id)}:
        raise CacheValidationError(
            f"orbit cache for {expected_norad_id} contains records for another satellite"
        )
    return ids


def _validated_records(records, expected_norad_id: int) -> pd.DataFrame:
    """Return a validated record frame belonging entirely to one NORAD ID.

    Checked row by row rather than column by column, because one file may hold
    both kinds: a column-level null check would reject a TLE row for having no
    ``MEAN_MOTION`` and an OMM row for having no ``TLE_LINE1``, when neither is
    a defect.
    """
    frame = pd.DataFrame(records)
    if frame.empty:
        raise CacheValidationError("orbit cache has no usable records")
    frame["NORAD_CAT_ID"] = _validated_ids(frame, expected_norad_id)

    for _, row in frame.iterrows():
        try:
            kind = record_kind(row)
            missing = [
                column
                for column in REQUIRED_COLUMNS_BY_KIND[kind]
                if column not in frame.columns or pd.isna(row[column])
            ]
            if missing:
                raise ValueError(f"{kind} record is missing {', '.join(missing)}")
            embedded_id = validate_record(row)
        except (TypeError, ValueError) as error:
            raise CacheValidationError(
                f"invalid record for {expected_norad_id}: {error}"
            ) from error
        if embedded_id != int(expected_norad_id):
            raise CacheValidationError(
                f"record belongs to satellite {embedded_id}, not {expected_norad_id}"
            )
    return frame.reset_index(drop=True)


def _checked_search_name(name) -> str:
    """The same rule :func:`~satchecker_client.client.search_satellites` applies."""
    if not isinstance(name, str):
        raise TypeError(f"name must be a string, not {type(name).__name__}")
    if not name.strip():
        raise ValueError("name must not be empty")
    return name


def _search_key(name: str, base_url: str) -> str:
    """Digest of everything that decides a search's result: service, endpoint, name."""
    material = json.dumps(
        {"base_url": base_url, "endpoint": _SEARCH_ENDPOINT, "name": name},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _validated_search_frame(found) -> pd.DataFrame:
    """A search result checked the way the client checks a reply, as a clean frame.

    Raises :class:`CacheValidationError`. The rules are the ones
    :func:`~satchecker_client.client.search_satellites` applies to the service's
    reply: integer IDs, a name on every row, and dates that are ``YYYY-MM-DD``
    or null — exactly, since callers compare them as strings.
    """
    frame = pd.DataFrame(found) if not isinstance(found, pd.DataFrame) else found
    if frame.empty:
        missing = [column for column in SEARCH_COLUMNS if column not in frame.columns]
        if len(frame.columns) and missing:
            raise CacheValidationError(f"search result is missing columns {missing}")
        return pd.DataFrame(columns=SEARCH_COLUMNS)
    missing = [column for column in SEARCH_COLUMNS if column not in frame.columns]
    if missing:
        raise CacheValidationError(f"search result is missing columns {missing}")
    frame = frame[SEARCH_COLUMNS].reset_index(drop=True).copy()

    ids = frame["NORAD_CAT_ID"]
    if ids.isnull().any():
        raise CacheValidationError("search result has null values in NORAD_CAT_ID")
    try:
        numeric = pd.to_numeric(ids)
    except (TypeError, ValueError) as error:
        raise CacheValidationError(f"search result NORAD_CAT_ID is not numeric: {error}") from error
    if any(
        isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) != round(float(value))
        or float(value) <= 0
        for value in numeric
    ):
        raise CacheValidationError(
            "search result NORAD_CAT_ID contains non-positive, non-finite or "
            "non-integer values"
        )
    frame["NORAD_CAT_ID"] = numeric.astype(int)

    for column in SEARCH_COLUMNS[1:]:
        for value in frame[column]:
            if pd.isna(value):
                if column == "OBJECT_NAME":
                    raise CacheValidationError("search result has a row with no OBJECT_NAME")
                continue
            if not isinstance(value, str):
                raise CacheValidationError(
                    f"search result {column} holds a non-string value {value!r}"
                )
            if column in ("LAUNCH_DATE", "DECAY_DATE") and not is_iso_date(value):
                raise CacheValidationError(
                    f"search result {column} is not a YYYY-MM-DD date: {value!r}"
                )
    return frame


def _search_records_for_json(frame: pd.DataFrame) -> list[dict]:
    """Rows as plain JSON values: native ints, and null — never NaN — for missing."""
    return [
        {
            column: (
                int(row[column])
                if column == "NORAD_CAT_ID"
                else (None if pd.isna(row[column]) else row[column])
            )
            for column in SEARCH_COLUMNS
        }
        for _, row in frame.iterrows()
    ]


def _snapshot_from_envelope(envelope, name: str, base_url: str) -> SearchSnapshot:
    """Check a search file's envelope against the search asked for; build the snapshot."""
    if not isinstance(envelope, dict):
        raise CacheValidationError("search cache envelope is not an object")
    if envelope.get("schema_version") != SEARCH_SCHEMA_VERSION:
        raise CacheValidationError(
            f"unsupported schema_version {envelope.get('schema_version')!r} "
            f"(this satchecker_client writes {SEARCH_SCHEMA_VERSION})"
        )
    if envelope.get("endpoint") != _SEARCH_ENDPOINT:
        raise CacheValidationError(
            f"search cache file is for endpoint {envelope.get('endpoint')!r}"
        )
    if envelope.get("base_url") != base_url:
        raise CacheValidationError(
            f"search cache file is for service {envelope.get('base_url')!r}, "
            f"not {base_url!r}"
        )
    if envelope.get("name") != name:
        raise CacheValidationError(
            f"search cache file is for {envelope.get('name')!r}, not {name!r}"
        )
    try:
        fetched_at = datetime.strptime(envelope.get("fetched_at"), _FETCHED_AT_FORMAT)
    except (TypeError, ValueError) as error:
        raise CacheValidationError(
            f"search cache fetched_at is unreadable: {envelope.get('fetched_at')!r}"
        ) from error
    records = envelope.get("records")
    if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
        raise CacheValidationError("search cache records are not a list of objects")
    count = envelope.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count != len(records):
        raise CacheValidationError(
            f"search cache count {count!r} does not match its {len(records)} records"
        )
    if any(set(row) != set(SEARCH_COLUMNS) for row in records):
        raise CacheValidationError("search cache records do not carry the search columns")
    frame = (
        _validated_search_frame(pd.DataFrame(records, columns=SEARCH_COLUMNS))
        if records
        else pd.DataFrame(columns=SEARCH_COLUMNS)
    )
    return SearchSnapshot(
        name=name, found=frame, fetched_at=fetched_at.replace(tzinfo=timezone.utc)
    )


def _cacheable(records: pd.DataFrame, norad_id: int) -> pd.DataFrame:
    """*records* as the shared cache may hold them.

    The cache file is read by every application using this package, at whatever
    version each is on, and 0.1.x rejects a whole file — verified records
    included — on meeting a TLE line it cannot validate, then overwrites it on its
    next store. So a TLE with no checksum digit is left out, and one repaired of a
    stray backslash is written with its lines in standard form. Both are also
    what any other reader would want: the first cannot be verified at all.

    Only a record that is valid — allowing for those two defects, and belonging
    to *norad_id* by both its row ID and the ID in its lines — is left out or
    repaired here. Anything else passes through
    untouched, for :func:`_validated_records` to reject with a
    :class:`CacheValidationError` as it always has.
    """
    keep = []
    frame = records.reset_index(drop=True).copy()
    for index, row in frame.iterrows():
        try:
            is_tle = record_kind(row) == KIND_TLE
            lines = (row["TLE_LINE1"], row["TLE_LINE2"]) if is_tle else ()
        except (KeyError, TypeError, ValueError):
            keep.append(index)
            continue
        if not is_tle or not all(isinstance(line, str) for line in lines):
            keep.append(index)
            continue
        defects = set(tle_line_defects(lines[0])) | set(tle_line_defects(lines[1]))
        if not defects:
            keep.append(index)
            continue
        try:
            # The row's own ID as well as the one in its lines: validate_record
            # reads only the lines. Checked by _validated_ids itself, the rule
            # _validated_records applies afterwards, so no row ID it would reject
            # — missing, null, non-numeric, fractional, non-positive or another
            # satellite's — can be skipped here instead.
            _validated_ids(
                pd.DataFrame({"NORAD_CAT_ID": [row["NORAD_CAT_ID"]]}, dtype=object),
                norad_id,
            )
            if validate_record(row, allow_missing_checksum=True) != int(norad_id):
                raise ValueError("record belongs to another satellite")
            standard = (
                validate_tle_line(lines[0], 1, allow_missing_checksum=True),
                validate_tle_line(lines[1], 2, allow_missing_checksum=True),
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            keep.append(index)
            continue
        if MISSING_CHECKSUM in defects:
            continue
        frame.at[index, "TLE_LINE1"], frame.at[index, "TLE_LINE2"] = standard
        keep.append(index)
    return frame.loc[keep].reset_index(drop=True)


def _drop_duplicates_per_kind(frame: pd.DataFrame) -> pd.DataFrame:
    """Deduplicate within each kind, on the columns that identify that kind.

    One set of dedupe columns cannot serve both: dedupe on the TLE lines and
    every OMM record collapses to one (they all have null lines); dedupe on
    ``EPOCH`` and two genuinely different TLEs published for the same instant
    would be merged.
    """
    if frame.empty:
        return frame
    kinds = frame.apply(_kind_for_grouping, axis=1)
    kept = []
    for kind, group in frame.groupby(kinds, sort=False):
        columns = [
            column
            for column in DEDUPE_COLUMNS_BY_KIND.get(kind, ())
            if column in group.columns
        ]
        kept.append(group.drop_duplicates(subset=columns, keep="last") if columns else group)
    return pd.concat(kept).sort_index()


#: Group label for a record whose kind cannot be determined. It must survive
#: grouping rather than be dropped here: _validated_records is what rejects it,
#: and it does so with a message that says why.
_UNKNOWN_KIND = "unknown"


def _kind_for_grouping(row) -> str:
    try:
        return record_kind(row)
    except ValueError:
        return _UNKNOWN_KIND


@contextmanager
def _exclusive_lock(path: Path):
    """Hold an exclusive inter-process lock on *path*'s ``.lock`` sidecar.

    Atomic replacement keeps a *reader* from ever seeing a partial file, but it
    does nothing for two concurrent read/merge/write transactions: both read
    the same old file and the last writer's replace discards the first's
    records — a real loss when separate jobs share the default user cache. The
    sidecar (never deleted; deleting a lock file is its own race) serialises
    the whole transaction. ``flock`` locks the open file description, so this
    also serialises threads within one process.
    """
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as handle:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        else:  # msvcrt: locks one byte; LK_LOCK retries ~10s then raises OSError
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_UN)
            else:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write *payload* atomically so a partial cache file is never observed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class TextOrbitCache:
    """Human-readable, per-NORAD JSON cache under one directory.

    One file per satellite, holding records of either kind. Around the archive
    handover a file will typically hold both: the satellite's last TLEs and its
    first OMM element sets.
    """

    def __init__(self, cache_dir):
        # expanduser so "~/.cache/..." means what every caller writing it meant.
        self.cache_dir = Path(cache_dir).expanduser()

    def path(self, norad_id: int) -> Path:
        return self.cache_dir / f"orbit-{int(norad_id)}.json"

    def get(self, norad_id: int, log: Callable[[str], None] = print) -> pd.DataFrame:
        """Return all validated cached records for *norad_id*, or an empty frame.

        An absent file is an ordinary miss and says nothing. A file that exists
        but cannot be used is reported: silently treating it as a miss costs a
        network request on every run with nothing to indicate why, and a cache
        that never takes hold is otherwise invisible.

        This is also the whole migration path for the schema bump. A v1 file
        fails the version check, gets reported as unusable, and is overwritten by
        the next successful fetch — so nothing has to convert it.
        """
        path = self.path(norad_id)
        if not path.exists():
            return pd.DataFrame()
        try:
            with open(path) as handle:
                envelope = json.load(handle)
            if not isinstance(envelope, dict):
                raise CacheValidationError("orbit cache envelope is not an object")
            if envelope.get("schema_version") != SCHEMA_VERSION:
                raise CacheValidationError(
                    f"unsupported schema_version {envelope.get('schema_version')!r} "
                    f"(this satchecker_client writes {SCHEMA_VERSION})"
                )
            if envelope.get("norad_id") != int(norad_id):
                raise CacheValidationError("orbit cache envelope has the wrong NORAD ID")
            return _validated_records(envelope.get("records") or [], int(norad_id))
        except (OSError, ValueError, TypeError) as error:
            log(
                f"  warning: cached orbit file {path} is unusable ({error}); "
                "treating it as a cache miss"
            )
            return pd.DataFrame()

    def store(self, norad_id: int, records: pd.DataFrame) -> None:
        """Merge newly fetched immutable records into one satellite's cache.

        Concatenating kinds widens the frame — a TLE row gains null element
        columns and an OMM row gains null lines — which is why deduplication and
        validation are both per-row rather than per-column.

        TLE records with no checksum digit are not written, and a batch made up
        only of those leaves the file untouched; see :func:`_cacheable`.
        """
        if records.empty:
            return
        norad_id = int(norad_id)
        incoming = _cacheable(records, norad_id)
        if incoming.empty:
            return
        if "FETCHED_AT" not in incoming.columns:
            incoming["FETCHED_AT"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        # The read, merge and write are one transaction: without the lock, two
        # concurrent stores both read the same old file and the later replace
        # silently discards the earlier writer's records.
        with _exclusive_lock(self.path(norad_id)):
            existing = self.get(norad_id)
            merged = (
                pd.concat([existing, incoming], ignore_index=True)
                if not existing.empty
                else incoming
            )
            merged = _drop_duplicates_per_kind(merged)
            merged = _validated_records(merged, norad_id)
            envelope = {
                "schema_version": SCHEMA_VERSION,
                "norad_id": norad_id,
                "records": merged.to_dict(orient="records"),
            }
            _atomic_write_json(self.path(norad_id), envelope)

    def search_path(self, name: str) -> Path:
        """Where the cached result of searching for *name* lives.

        Keyed by the name exactly as sent, since the service matches it
        case-sensitively and reads ``%`` and ``_`` as wildcards: ``NAVSTAR`` and
        ``navstar`` are different searches with different results. The key is a
        digest because a name can hold characters a filename cannot.

        The service is part of the key too, read from ``client.BASE_URL`` as
        :func:`~satchecker_client.client.search_satellites` reads it, so results
        from a client pointed at another service are cached apart. Each method
        reads it once; changing it partway through a lookup, fetch and store is
        not supported (see the usage guide).
        """
        return self._search_file(_checked_search_name(name), client.BASE_URL)

    def _search_file(self, name: str, base_url: str) -> Path:
        return self.cache_dir / f"search-{_search_key(name, base_url)}.json"

    def get_search(
        self, name: str, log: Callable[[str], None] = print
    ) -> Optional[SearchSnapshot]:
        """Return the cached search for *name*, or ``None`` on a miss.

        Follows :meth:`get`: an absent file is an ordinary miss and says nothing,
        and a file that exists but cannot be used is reported and treated as a
        miss. A cached search that matched nothing is not a miss — it comes back
        as a :class:`SearchSnapshot` with an empty ``found``.

        Nothing here judges age. A search result goes stale as the catalogue
        changes, in both directions: an old result can lack a decay date added
        since, keeping a satellite that should now be ruled out, and it can lack
        a satellite added to the catalogue or given a matching alias since.
        ``fetched_at`` is there for the caller's policy.
        """
        name = _checked_search_name(name)
        base_url = client.BASE_URL  # once, so the file read and the check agree
        path = self._search_file(name, base_url)
        if not path.exists():
            return None
        try:
            with open(path) as handle:
                envelope = json.load(handle)
            return _snapshot_from_envelope(envelope, name, base_url)
        except (OSError, ValueError, TypeError) as error:
            log(
                f"  warning: cached search file {path} is unusable ({error}); "
                "treating it as a cache miss"
            )
            return None

    def store_search(
        self, name: str, found: pd.DataFrame, fetched_at: Optional[datetime] = None
    ) -> None:
        """Replace the cached result of searching for *name* with *found*.

        *found* is a :func:`~satchecker_client.client.search_satellites` result,
        empty or not; *fetched_at* defaults to now, and a naive datetime is taken
        as UTC. Unlike :meth:`store`, this replaces rather than merges: a search
        result is a snapshot of a catalogue that changes, so rows from an older
        search are not evidence about a newer one. An invalid *found* raises
        :class:`CacheValidationError` and leaves any existing file untouched.
        """
        name = _checked_search_name(name)
        base_url = client.BASE_URL  # once, so the file written and its label agree
        frame = _validated_search_frame(found)
        if fetched_at is None:
            fetched_at = datetime.now(timezone.utc)
        elif fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        records = _search_records_for_json(frame)
        envelope = {
            "schema_version": SEARCH_SCHEMA_VERSION,
            "endpoint": _SEARCH_ENDPOINT,
            "base_url": base_url,
            "name": name,
            "fetched_at": fetched_at.astimezone(timezone.utc).strftime(_FETCHED_AT_FORMAT),
            "count": len(records),
            "records": records,
        }
        path = self._search_file(name, base_url)
        with _exclusive_lock(path):
            _atomic_write_json(path, envelope)


#: Columns worth carrying out of an explicit user/replay file. Everything a
#: record of either kind is validated and resolved from, and nothing else.
_LEGACY_KEEP_COLUMNS = (
    "NORAD_CAT_ID",
    "OBJECT_NAME",
    "OBJECT_ID",
    KIND_FIELD,
    "TLE_LINE1",
    "TLE_LINE2",
    "EPOCH",
    *OMM_ELEMENT_COLUMNS,
)


def read_legacy_tle_records(directory) -> pd.DataFrame:
    """Read explicit user/replay ``*.json`` orbit tables from *directory*.

    A file qualifies if it exposes the required columns for *either* kind, so a
    Space-Track ``gp`` / ``gp_history`` export drops in unconverted whether it
    carries TLE lines, OMM element columns, or — as those exports usually do —
    both. No ``RECORD_KIND`` is needed: it is inferred, and a file carrying both
    resolves as a TLE, whose lines are the stronger thing to validate against.

    ``EPOCH`` is kept for the OMM records that have no other epoch. It is still
    ignored for TLEs, which re-derive theirs from line 1.

    Managed ``orbit-<NORAD>.json`` envelopes are intentionally not interpreted as
    explicit input.

    Floats are decoded with pandas' correctly-rounded parser, so every finite
    value down to the smallest normal double reads back as the double that was
    written. Two limits remain, both pandas' rather than this reader's. A
    subnormal magnitude — anything below 2.2250738585072014e-308 — can make
    pandas raise, and the file is then skipped like any other unreadable one,
    even when the value sits in a column this reader would have discarded. No
    orbital element comes anywhere near that range. And pandas still infers
    column types after decoding: a column whose values are all integral comes
    back as integers, and ``-0.0`` loses its sign. This is accurate decoding of
    each value, not a bit-for-bit round trip of a DataFrame.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return pd.DataFrame()
    frames = []
    for path in sorted(glob(str(directory / "*.json"))):
        try:
            frame = pd.read_json(path, precise_float=True)
        except (ValueError, OSError):
            continue
        if not any(
            all(column in frame.columns for column in required)
            for required in REQUIRED_COLUMNS_BY_KIND.values()
        ):
            continue
        keep = [column for column in _LEGACY_KEEP_COLUMNS if column in frame.columns]
        frames.append(frame[keep].copy())
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
