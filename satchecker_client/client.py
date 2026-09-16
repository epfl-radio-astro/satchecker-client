"""Client for the IAU CPS SatChecker nearest-element service.

Self-contained transport layer: this module talks to the SatChecker HTTP API and
returns pandas DataFrames with a normalised column set. No account or credentials
are required. It knows nothing about caching, cache-key policy, or
source-precedence policy — the first lives in :mod:`satchecker_client.cache` and
the rest are the caller's.

Two endpoints, because SatChecker keeps two archives:

``GET /tools/get-nearest-tle/``
    The TLE whose epoch is closest to the requested one. This archive is frozen:
    its last record is from 2026-07-11 and it will never gain another.
``GET /tools/get-nearest-omm/``
    The OMM element set whose epoch is closest to the requested one. This
    archive begins at the handover, twelve hours after the TLE archive ends, and
    grows forward only.

SatChecker 1.7.0 made the split because Celestrak is dropping Alpha-5 notation
to preserve the original TLE format, which leaves catalogue numbers above 99999
with no TLE representation at all. Which endpoint to ask is
:mod:`satchecker_client.service`'s decision, not this module's.

A third endpoint answers a different question — which satellites exist, rather
than where one of them is:

``GET /tools/search-satellites/``
    Catalogue entries whose name contains a given string. See
    :func:`search_satellites` for what a match does and does not identify.

Neither archive reports "I have nothing that old". ``get-nearest-omm`` answers a
2021 request with its earliest 2026-07-11 record, 4.6 years off epoch, and says
nothing about the discrepancy. Callers are expected to check the epoch they got
against the epoch they asked for; this module only reports what came back.

Returned frames use OMM-style column names throughout. Both kinds carry
``NORAD_CAT_ID``, ``OBJECT_NAME``, ``EPOCH``, ``DATA_SOURCE``,
``DATE_COLLECTED`` and ``RECORD_KIND``; TLE frames add ``TLE_LINE1`` /
``TLE_LINE2``, and OMM frames add ``OBJECT_ID`` and the seven element columns.
"""

from __future__ import annotations

import email.utils
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from ._version import __version__

from ._time import datetime_to_jd, is_iso_date
from .records import KIND_FIELD, KIND_OMM, KIND_TLE, OMM_ELEMENT_COLUMNS


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://satchecker.cps.iau.org/tools"
# Identify ourselves to the SatChecker operators, with a contact URL. The
# service is run as a courtesy to the community and is rate limited, so a
# request that cannot be attributed is a request its operators cannot reason
# about.
USER_AGENT = (
    f"satchecker-client/{__version__} "
    "(+https://github.com/epfl-radio-astro/satchecker-client)"
)

#: Appended to :data:`USER_AGENT` by :func:`set_client_identifier`. Set by the
#: consuming application, not by this package.
_client_identifier: Optional[str] = None
REQUEST_TIMEOUT = 120  # seconds


def set_client_identifier(identifier: Optional[str]) -> None:
    """Name the consuming application in this package's outgoing User-Agent.

    Without this every caller looks identical to the SatChecker operators, which
    matters because the library is shared: a burst of traffic they want to ask
    about should be traceable to the application that made it rather than to the
    client library that all of them use. Call it once at import or startup::

        satchecker_client.set_client_identifier("tabascal/0.1.0")

    Pass ``None`` to clear it.
    """
    global _client_identifier
    _client_identifier = identifier.strip() if identifier else None


def user_agent() -> str:
    """The full User-Agent sent with each request, caller identity included."""
    if _client_identifier:
        return f"{USER_AGENT} {_client_identifier}"
    return USER_AGENT

#: Where SatChecker stopped publishing TLEs and started publishing OMM. Its
#: 1.7.0 changelog: "TLEs for dates before 2026-07-12 will continue to be used as
#: is with no changes to any of the related endpoints, but OMM will be used for
#: everything going forward."
#:
#: A *hint*, not a contract. It picks which endpoint to ask first and nothing
#: else; being wrong about it costs one extra request, because the caller falls
#: back to the other endpoint when the first yields nothing acceptable. That
#: matters because the boundary can move: SatChecker now sources OMM from
#: Space-Track as well as Celestrak, and Space-Track's OMM history runs years
#: deep, so a backfill is plausible. A hardcoded permanent cutoff would keep
#: silently preferring TLEs for periods where better OMM had appeared.
#:
#: Deliberately not a config key: it is a property of the service, not of a run.
HANDOVER_JD = datetime_to_jd(datetime(2026, 7, 12))

# Columns the normalised TLE frames expose.
TLE_COLUMNS = [
    "NORAD_CAT_ID",
    "OBJECT_NAME",
    "EPOCH",
    "TLE_LINE1",
    "TLE_LINE2",
    "DATA_SOURCE",
    "DATE_COLLECTED",
]

# Columns the normalised OMM frames expose.
OMM_COLUMNS = [
    "NORAD_CAT_ID",
    "OBJECT_NAME",
    "OBJECT_ID",
    "EPOCH",
    *OMM_ELEMENT_COLUMNS,
    "DATA_SOURCE",
    "DATE_COLLECTED",
]

# Columns the normalised catalogue-search frames expose. ``OBJECT_ID`` is the
# international (COSPAR) designator, as it is in the OMM frames; the two dates are
# ISO ``YYYY-MM-DD`` strings, or null where the catalogue has none.
SEARCH_COLUMNS = [
    "NORAD_CAT_ID",
    "OBJECT_NAME",
    "OBJECT_ID",
    "OBJECT_TYPE",
    "RCS_SIZE",
    "LAUNCH_DATE",
    "DECAY_DATE",
]

# SatChecker response field -> normalised column name.
_FIELD_RENAME = {
    "satellite_id": "NORAD_CAT_ID",
    "satellite_name": "OBJECT_NAME",
    "epoch": "EPOCH",
    "tle_line1": "TLE_LINE1",
    "tle_line2": "TLE_LINE2",
    "data_source": "DATA_SOURCE",
    "date_collected": "DATE_COLLECTED",
}

# The same, for the OMM rows. The row-level ``epoch`` is deliberately *not*
# mapped: SatChecker spells it "2026-08-13 03:34:14 UTC" there, which is neither
# ISO 8601 nor sub-second, while the nested element object carries the same
# instant as "2026-08-13T03:34:14.082240". The nested one is what we lift.
_FIELD_RENAME_OMM = {
    "satellite_id": "NORAD_CAT_ID",
    "satellite_name": "OBJECT_NAME",
    "data_source": "DATA_SOURCE",
    "date_collected": "DATE_COLLECTED",
}

_FIELD_RENAME_SEARCH = {
    "satellite_id": "NORAD_CAT_ID",
    "satellite_name": "OBJECT_NAME",
    "international_designator": "OBJECT_ID",
    "object_type": "OBJECT_TYPE",
    "rcs_size": "RCS_SIZE",
    "launch_date": "LAUNCH_DATE",
    "decay_date": "DECAY_DATE",
}

# Fields lifted out of each row's nested ``orbital_elements`` object. SatChecker
# already names them in OMM style, so this is a move rather than a translation.
# The object also carries CLASSIFICATION_TYPE, ELEMENT_SET_NO, EPHEMERIS_TYPE,
# MEAN_MOTION_DOT, MEAN_MOTION_DDOT and REV_AT_EPOCH; nothing downstream reads
# them, so they are dropped rather than stored and never used.
_OMM_LIFTED_FIELDS = ("EPOCH", "OBJECT_ID", *OMM_ELEMENT_COLUMNS)


class SatCheckerError(RuntimeError):
    """Raised when SatChecker cannot be reached or returns no usable data.

    The two subclasses below separate the failures a caller can usefully route
    around from the ones it cannot; catching this base class treats them alike and
    is only appropriate at a top-level boundary.
    """


class SatCheckerTransportError(SatCheckerError):
    """The service could not be reached: connection, TLS, timeout or mid-read failure.

    Whole-service, not per-request: every other satellite's lookup would go to the
    same unreachable host. :func:`satchecker_client.service.fetch_nearest_batch`
    treats the first one as the answer for the whole batch and abandons the
    requests still queued, rather than paying the request timeout once per
    configured satellite to learn the same thing.
    """


class SatCheckerRateLimitError(SatCheckerTransportError):
    """The service asked this client to slow down (HTTP 429).

    A *transport* error by classification, because the thing it tells us is about
    the service and not about the satellite we happened to ask for: the next
    request is unwelcome too. Being a subclass is what stops a batch dead on the
    first 429 instead of working through the rest of the list.

    ``retry_after`` carries the service's own ``Retry-After`` hint in seconds when
    it sends one, so the message can say when the run is worth repeating. This
    package reports the hint rather than sleeping on it: an unattended run that
    quietly blocks for an interval the service chose is worse than one that stops
    and says why. Whether to wait is the caller's call.
    """

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class SatCheckerResponseError(SatCheckerError):
    """The service answered, but the response is unusable: malformed or incomplete.

    Per-request, not whole-service: the host is up and answering, so it says
    nothing about the other satellites. Callers record it against the one ID and
    carry on with the rest of the batch.

    ``status`` is the HTTP status when the failure came from one (``None`` for a
    malformed body). A run of identical statuses with no success in between is
    how a caller recognises that "per-request" has stopped being true — a WAF
    answering 403 to everything, or a renamed endpoint answering 404.
    """

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Low-level HTTP + normalisation
# ---------------------------------------------------------------------------

# HTTP statuses that mean service-level backoff rather than a bad individual request.
_BACKOFF_STATUSES = frozenset({429})


def _retry_after_seconds(error: urllib.error.HTTPError) -> Optional[float]:
    """Seconds to wait, from a ``Retry-After`` header in either permitted form.

    RFC 9110 allows delta-seconds (``120``) or an HTTP-date
    (``Wed, 21 Oct 2026 07:28:00 GMT``); both appear in the wild. An absent,
    malformed or already-elapsed value yields ``None`` / ``0.0`` rather than an
    exception — a bad hint must never turn into a second failure on top of the
    one being reported.
    """
    headers = getattr(error, "headers", None)
    raw = headers.get("Retry-After") if headers is not None else None
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        stamp = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if stamp is None:
        return None
    if stamp.tzinfo is None:  # an HTTP-date without a zone is GMT
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (stamp - datetime.now(timezone.utc)).total_seconds())


def _status_error(url: str, error: urllib.error.HTTPError) -> SatCheckerError:
    """Classify an HTTP status response as a response or a transport failure.

    An HTTP status means the service *answered*, so it is not automatically a
    transport failure — but it is only worth trying a different endpoint when the
    server was rejecting this particular request rather than failing wholesale:

    * 4xx (except 429) — this individual request was rejected.
    * 429 — the service is asking this client to back off, and says so about the
      client rather than about the satellite requested.
    * 5xx — the service is failing server-side.
    """
    status = getattr(error, "code", None)
    detail = f"SatChecker returned HTTP {status} ({url}): {error.reason}"
    if status in _BACKOFF_STATUSES:
        retry_after = _retry_after_seconds(error)
        hint = (
            f"; it asks for {retry_after:g} s before the next request"
            if retry_after is not None
            else ""
        )
        return SatCheckerRateLimitError(
            f"SatChecker returned HTTP {status} — it is rate-limiting this "
            f"client{hint} ({url}): {error.reason}",
            retry_after=retry_after,
        )
    if status is not None and 400 <= status < 500:
        return SatCheckerResponseError(detail, status=status)
    return SatCheckerTransportError(detail)


def _http_get(url: str, timeout: int = REQUEST_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        # Must precede URLError: HTTPError subclasses it, and unlike its siblings
        # it means the service replied rather than that it could not be reached.
        raise _status_error(url, e) from e
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        http.client.HTTPException,
    ) as e:
        raise SatCheckerTransportError(f"SatChecker request failed ({url}): {e}") from e


def _load_json(raw: bytes, url: str):
    """Parse a JSON response, wrapping malformed payloads as ``SatCheckerError``."""
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as e:
        raise SatCheckerResponseError(f"SatChecker returned invalid JSON ({url}): {e}") from e


def _as_object(payload, url: str) -> dict:
    """Return the JSON object carrying the response fields.

    SatChecker sometimes wraps the payload in a single-element list. An empty list
    or a scalar is a malformed response for the endpoints that read named fields,
    so raise ``SatCheckerError`` rather than letting a raw ``IndexError`` /
    ``AttributeError`` escape.
    """
    obj = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(obj, dict):
        raise SatCheckerResponseError(
            f"SatChecker returned an unexpected response shape ({url}): "
            f"{type(payload).__name__}"
        )
    return obj


def _checked_ids(df: pd.DataFrame) -> pd.Series:
    """Satellite IDs from a normalised frame, as usable integers.

    Shared by both kinds: whatever the record format, an ID that is absent,
    non-numeric, non-finite or fractional makes the row unusable in the same way
    — a fractional ID would silently truncate to a *different* satellite, and an
    infinity raises inside ``astype``.
    """
    try:
        ids = pd.to_numeric(df["NORAD_CAT_ID"])
    except (ValueError, TypeError) as e:
        raise SatCheckerResponseError(
            f"SatChecker response has non-numeric satellite IDs: {e}"
        ) from e
    if ids.isnull().any():
        raise SatCheckerResponseError(
            "SatChecker response is missing satellite IDs (satellite_id)"
        )
    # Require finite integers before casting: a fractional ID would silently
    # truncate to a different satellite, and infinities raise inside astype().
    if not np.isfinite(ids.to_numpy(dtype=float)).all():
        raise SatCheckerResponseError("SatChecker response has non-finite satellite IDs")
    if (ids != ids.round()).any():
        bad = ids[ids != ids.round()].unique()[:5]
        raise SatCheckerResponseError(
            f"SatChecker response has non-integer satellite IDs: {list(bad)}"
        )
    try:
        return ids.astype(int)
    except (ValueError, TypeError, OverflowError) as e:
        raise SatCheckerResponseError(f"SatChecker satellite IDs are not usable: {e}") from e


def _project(records: pd.DataFrame, columns: list[str], kind: str) -> pd.DataFrame:
    """Reduce a raw frame to *columns*, stamping the record kind onto every row.

    Missing columns are filled rather than raising, so a response that simply
    omits an optional field stays usable; the required ones are checked by the
    per-kind normalisers below.
    """
    df = records.copy()
    for col in columns:
        if col not in df.columns:
            df[col] = None
    df = df[columns].copy()
    # Stated rather than inferred. Inference exists for user-supplied files that
    # cannot carry the field; a response we parsed ourselves knows what it is.
    df[KIND_FIELD] = kind
    return df


def _normalise(records: pd.DataFrame) -> pd.DataFrame:
    """Rename SatChecker fields to the normalised TLE columns.

    Response rows are validated here so schema problems surface as
    :class:`SatCheckerError` (the module's error contract) rather than as raw
    pandas exceptions: satellite IDs must be present and numeric, and both TLE
    lines must be present.
    """
    df = _project(records.rename(columns=_FIELD_RENAME), TLE_COLUMNS, KIND_TLE)
    df["NORAD_CAT_ID"] = _checked_ids(df)
    for col in ("TLE_LINE1", "TLE_LINE2"):
        if df[col].isnull().any():
            raise SatCheckerResponseError(f"SatChecker response is missing {col} values")
    return df.reset_index(drop=True)


def _lift_orbital_elements(rows: list[dict], url: str) -> list[dict]:
    """Flatten each row's nested ``orbital_elements`` object onto the row itself.

    SatChecker nests the elements one level down and already names them in OMM
    style, so this is a move rather than a translation. Doing it before the
    frame is built keeps a nested ``dict`` out of a pandas cell, where it would
    survive every column check and only fail much later.
    """
    lifted = []
    for row in rows:
        nested = row.get("orbital_elements")
        if not isinstance(nested, dict):
            raise SatCheckerResponseError(
                f"SatChecker OMM row has no orbital_elements object ({url}): "
                f"{type(nested).__name__}"
            )
        flat = {key: value for key, value in row.items() if key != "orbital_elements"}
        for field in _OMM_LIFTED_FIELDS:
            if field in nested:
                flat[field] = nested[field]
        lifted.append(flat)
    return lifted


def _normalise_omm(records: pd.DataFrame) -> pd.DataFrame:
    """Rename SatChecker fields to the normalised OMM columns.

    The same contract as :func:`_normalise` — schema problems surface as
    :class:`SatCheckerError` — over a different required set. There is no
    checksum and no second copy of the identifier to verify here; range and
    finiteness checks on the elements happen in
    :mod:`satchecker_client.records`, which is also where the reasoning about
    that gap lives.
    """
    df = _project(records.rename(columns=_FIELD_RENAME_OMM), OMM_COLUMNS, KIND_OMM)
    df["NORAD_CAT_ID"] = _checked_ids(df)
    for col in ("EPOCH", *OMM_ELEMENT_COLUMNS):
        if df[col].isnull().any():
            raise SatCheckerResponseError(f"SatChecker response is missing {col} values")
    return df.reset_index(drop=True)


# Fields a ``get-nearest-*`` reply may carry its rows in. ``tle_data`` is the
# older spelling, still served by some deployments and still accepted.
_NEAREST_DATA_FIELDS = ("orbital_data", "tle_data")


def _strict_nearest_data(obj: dict, endpoint: str, url: str):
    """The rows of a nearest-record envelope, required in full rather than defaulted.

    The reasoning is :func:`_search_rows`': every lenient reading of a broken
    reply ends the same way, as "this satellite has no record" — which is also
    what a genuinely empty archive says, and acting on it drops a requested
    satellite from a run with nothing to show for it. So the envelope must carry
    no ``error``, at least one recognised data field, and rows in that field
    rather than some falsy value standing in for them.

    Fields are selected by *presence*, not by truthiness, which is what makes a
    null or empty-string ``orbital_data`` distinguishable from an absent one. Two
    recognised fields with different contents describe two different answers to
    one question, so neither is used.
    """
    if "error" in obj:
        raise SatCheckerResponseError(
            f"SatChecker {endpoint} response reports an error ({url}): "
            f"{obj['error']!r}"
        )
    present = [field for field in _NEAREST_DATA_FIELDS if field in obj]
    if not present:
        raise SatCheckerResponseError(
            f"SatChecker {endpoint} response has no data field "
            f"({' or '.join(_NEAREST_DATA_FIELDS)}) ({url}): keys {sorted(obj)}"
        )
    field, rows = present[0], obj[present[0]]
    for other in present[1:]:
        if obj[other] != rows:
            raise SatCheckerResponseError(
                f"SatChecker {endpoint} response carries different {field} and "
                f"{other} ({url}): which one describes the satellite cannot be "
                "decided from the reply"
            )
    if isinstance(rows, dict):
        return [rows]
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise SatCheckerResponseError(
            f"SatChecker {endpoint} {field} is not a list of records ({url}): "
            f"{type(rows).__name__}"
        )
    return rows


def _nearest_rows(payload, endpoint: str, url: str, *, strict: bool):
    """Row dicts of a ``get-nearest-*`` reply, or ``None`` for "no such record".

    "No record" means *no record at all*: neither endpoint reports that it has
    nothing near the epoch asked for, and ``get-nearest-omm`` answers a 2021
    request with its earliest 2026 record. Judging the epoch is the caller's job.
    The service signals absence either as an empty top-level list or as an empty
    data field, both observed, and both readings keep those.

    Everything else the two readings disagree about. The default is lenient by
    history: it takes the first data field holding anything and reads an error
    envelope served with HTTP 200, a missing data field or a null one as absence
    too. *strict* separates those — see :func:`_strict_nearest_data`.
    """
    if isinstance(payload, list) and not payload:
        return None  # empty list == no record for this satellite
    if strict and isinstance(payload, list) and len(payload) != 1:
        raise SatCheckerResponseError(
            f"SatChecker {endpoint} response is a list of {len(payload)} objects, "
            f"not one ({url})"
        )
    obj = _as_object(payload, url)
    rows = (
        _strict_nearest_data(obj, endpoint, url)
        if strict
        else obj.get("orbital_data") or obj.get("tle_data") or []
    )
    if not rows:
        return None
    # The endpoint normally returns a list of row objects, but accepting a single
    # row object costs nothing and keeps pandas' raw "all scalar values" ValueError
    # from escaping the client's SatCheckerError contract.
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise SatCheckerResponseError(
            f"SatChecker returned unexpected {endpoint} rows ({url}): "
            f"{type(rows).__name__}"
        )
    return rows


def _fetch_nearest_rows(
    endpoint: str, norad_id: int, epoch_jd: float, *, strict: bool = False
):
    """Row dicts from one of the ``get-nearest-*`` endpoints, plus the URL used."""
    url = f"{BASE_URL}/{endpoint}/?" + urllib.parse.urlencode(
        {"id": int(norad_id), "id_type": "catalog", "epoch": repr(float(epoch_jd))}
    )
    payload = _load_json(_http_get(url), url)
    return _nearest_rows(payload, endpoint, url, strict=strict), url


def _frame(rows: list[dict], endpoint: str, url: str) -> pd.DataFrame:
    try:
        return pd.DataFrame.from_records(rows)
    except (ValueError, TypeError) as e:
        raise SatCheckerResponseError(
            f"SatChecker {endpoint} rows could not be read ({url}): {e}"
        ) from e


def _requested_rows(
    rows: list[dict], norad_id: int, endpoint: str, url: str
) -> list[dict]:
    """Only the raw rows belonging to the satellite that was asked for.

    The batch layer re-checks this, but the fetch functions are public on their
    own, and a misrouted response must not reach a direct caller labelled as the
    satellite it requested. Stray extra rows are dropped; a response containing
    *only* other satellites' records is an error rather than an empty frame —
    an empty frame states the catalogue has no record, and this response states
    no such thing. Filtering happens before full per-kind normalisation so a
    malformed stray row cannot invalidate a usable record for the requested
    satellite.
    """
    id_frame = pd.DataFrame(
        {"NORAD_CAT_ID": [row.get("satellite_id") for row in rows]}
    )
    ids = _checked_ids(id_frame)
    requested = int(norad_id)
    matching = [row for row, row_id in zip(rows, ids) if row_id == requested]
    if rows and not matching:
        others = sorted(set(int(value) for value in ids))
        raise SatCheckerResponseError(
            f"SatChecker {endpoint} returned records for satellite(s) {others}, "
            f"not the requested {requested} ({url})"
        )
    return matching


def fetch_nearest_tle(
    norad_id: int, epoch_jd: float, *, strict_response: bool = False
) -> pd.DataFrame:
    """Fetch the single TLE nearest *epoch_jd* for one satellite.

    Returns an empty DataFrame if SatChecker has no record for the satellite.
    Note that the TLE archive is frozen at 2026-07-11, so for any observation
    after that this returns the last TLE ever published for the satellite,
    however far from the requested epoch that is. Rows for other satellites are
    filtered out; a response carrying only those raises
    :class:`SatCheckerResponseError`.

    *strict_response* changes which replies are refused, not how an accepted one
    is read. By default a reply that makes no sense — an error envelope served
    with HTTP 200, no data field at all, a null one, several envelopes in one
    list — comes back as an empty frame, indistinguishable from the service
    saying it has no such record. Pass true and each of those raises
    :class:`SatCheckerResponseError` naming the endpoint, while the documented
    empty forms stay empty frames.
    """
    rows, url = _fetch_nearest_rows(
        "get-nearest-tle", norad_id, epoch_jd, strict=strict_response
    )
    if rows is None:
        return pd.DataFrame()
    rows = _requested_rows(rows, norad_id, "get-nearest-tle", url)
    return _normalise(_frame(rows, "get-nearest-tle", url))


def fetch_nearest_omm(
    norad_id: int, epoch_jd: float, *, strict_response: bool = False
) -> pd.DataFrame:
    """Fetch the single OMM element set nearest *epoch_jd* for one satellite.

    Returns an empty DataFrame if SatChecker has no record for the satellite.
    The OMM archive begins at the 2026-07-12 handover, and a request for an
    earlier epoch is answered with its *earliest* record rather than with
    nothing — so a pre-handover caller gets a confident-looking element set that
    may be years off. Rejecting that is the caller's age ceiling, not this
    function's — it reports what the service returned. Rows for other satellites
    are filtered out; a response carrying only those raises
    :class:`SatCheckerResponseError`.

    *strict_response* is as in :func:`fetch_nearest_tle`: by default a reply that
    makes no sense reads as the satellite having no record, and passing true
    raises :class:`SatCheckerResponseError` for it instead, keeping the
    documented empty forms empty. Distinguishing absence from an outage matters
    most here, since this archive is also the one that answers a pre-handover
    request with a record years off epoch.
    """
    rows, url = _fetch_nearest_rows(
        "get-nearest-omm", norad_id, epoch_jd, strict=strict_response
    )
    if rows is None:
        return pd.DataFrame()
    rows = _requested_rows(rows, norad_id, "get-nearest-omm", url)
    lifted = _lift_orbital_elements(rows, url)
    return _normalise_omm(_frame(lifted, "get-nearest-omm", url))


def _search_rows(payload, url: str) -> list[dict]:
    """The row objects of a ``search-satellites`` response, shape checked.

    The envelope is required in full rather than defaulted, because every way of
    reading a broken reply leniently ends the same way: as a search that matched
    nothing, silently dropping every satellite a caller asked for. So the reply
    must be one object (or a list holding exactly one), carry no ``error`` field,
    and carry both ``data`` and an integer ``count`` that agrees with it — the
    count being the only sign that a reply was cut short.
    """
    if isinstance(payload, list) and len(payload) != 1:
        raise SatCheckerResponseError(
            f"SatChecker search response is a list of {len(payload)} objects, "
            f"not one ({url})"
        )
    obj = _as_object(payload, url)
    if "error" in obj:
        raise SatCheckerResponseError(
            f"SatChecker search response reports an error ({url}): {obj['error']!r}"
        )
    if "data" not in obj:
        raise SatCheckerResponseError(
            f"SatChecker search response has no data field ({url}): "
            f"keys {sorted(obj)}"
        )
    rows = obj["data"]
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise SatCheckerResponseError(
            f"SatChecker returned unexpected search-satellites rows ({url}): "
            f"{type(rows).__name__}"
        )
    count = obj.get("count")
    if isinstance(count, bool) or not isinstance(count, int):
        raise SatCheckerResponseError(
            f"SatChecker search response has no integer count ({url}): {count!r}"
        )
    if count != len(rows):
        raise SatCheckerResponseError(
            f"SatChecker search response reports {count} matches but carries "
            f"{len(rows)} ({url})"
        )
    return rows


def _normalise_search(records: pd.DataFrame) -> pd.DataFrame:
    """Rename search fields to :data:`SEARCH_COLUMNS` and check each row.

    The same error contract as the record normalisers: a satellite ID must be a
    usable integer, a name must be present, and a date must be ``YYYY-MM-DD`` or
    null. Dates stay strings, as the service sends them; ISO dates compare
    correctly as strings, and a caller wanting an instant parses them itself.
    """
    df = records.rename(columns=_FIELD_RENAME_SEARCH)
    for col in SEARCH_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[SEARCH_COLUMNS].copy()
    df["NORAD_CAT_ID"] = _checked_ids(df)
    if df["OBJECT_NAME"].isnull().any():
        raise SatCheckerResponseError(
            "SatChecker search response is missing satellite names (satellite_name)"
        )
    for col in ("LAUNCH_DATE", "DECAY_DATE"):
        for value in df[col].dropna():
            if not is_iso_date(value):
                raise SatCheckerResponseError(
                    f"SatChecker search response has an unreadable {col} {value!r}"
                )
    return df.reset_index(drop=True)


def search_satellites(name: str) -> pd.DataFrame:
    """Catalogue entries whose name contains *name*, one row per entry as served.

    Returns a frame with :data:`SEARCH_COLUMNS`; empty, with those columns, when
    nothing matches. The match is SatChecker's, and it has sharp edges that this
    function reports rather than hides:

    - **Case-sensitive.** The catalogue is written almost entirely in upper case,
      so ``"starlink"`` matches nothing. Upper-casing the query covers most
      names, but not all: a few are mixed case, such as ``DMSat-1`` and
      ``OSCAR 9 (UoSAT 1)``, and no single spelling of a query matches every
      case variant.
    - ``%`` **and** ``_`` **are wildcards.** The service matches with SQL
      ``LIKE`` and does not escape them: ``%`` matches any run of characters and
      ``_`` any one character.
    - **A NORAD ID can appear on several rows**, one per name the entry is known
      by, and the rows need not carry the same fields: ``STARLINK-11691`` has a
      launch date and ``STARLINK-11691 [DTC]``, the same satellite, does not. To
      decide anything per satellite, combine its rows rather than taking the
      first.
    - **One object can appear under several NORAD IDs**, the same name and
      ``OBJECT_ID`` on each, for instance where a catalogue number was
      reassigned. Nothing in the response says which ID is current.
    - **Decayed objects are included**, with a ``DECAY_DATE``. Whether one
      belongs in a result depends on the epoch the caller cares about, so the
      filtering is left to the caller.

    An empty or all-whitespace *name* raises :class:`ValueError` without a
    request: SatChecker treats it as no filter at all and returns the entire
    catalogue, which is never what such a call meant.
    """
    if not isinstance(name, str):
        raise TypeError(f"name must be a string, not {type(name).__name__}")
    if not name.strip():
        raise ValueError(
            "name must not be empty: SatChecker treats an empty name as no filter "
            "and returns the entire catalogue"
        )
    url = f"{BASE_URL}/search-satellites/?" + urllib.parse.urlencode({"name": name})
    rows = _search_rows(_load_json(_http_get(url), url), url)
    if not rows:
        return pd.DataFrame(columns=SEARCH_COLUMNS)
    return _normalise_search(_frame(rows, "search-satellites", url))
