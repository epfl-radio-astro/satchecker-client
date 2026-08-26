# Usage guide

Everything below is importable from the top-level package:

```python
import satchecker_client as sc
```

## Identify your application

```python
sc.set_client_identifier("my-app/1.0 (+https://example.org/my-app)")
```

Optional, but do it. The service is run as a courtesy to the community and is
rate limited, so a burst of traffic its operators want to ask about should be
traceable to the application that made it rather than to the client library
every application shares. The identifier is appended to the package's own
`User-Agent`; {func}`~satchecker_client.client.user_agent` returns the combined
string being sent.

## Two archives, one handover

SatChecker keeps satellite orbits in two formats, and which one you get depends
on the epoch you ask about.

| Archive | Endpoint | Covers |
|---|---|---|
| TLE | `get-nearest-tle` | up to **2026-07-11**, frozen |
| OMM | `get-nearest-omm` | from **2026-07-12** onwards, growing |

The two do not overlap: the last TLE and the first OMM are about twelve hours
apart, and the TLE archive will never gain another record. SatChecker 1.7.0 made
the split because Celestrak is dropping Alpha-5 notation in order to preserve
the original TLE format — which means catalogue numbers above 99999 cease to be
representable as TLEs at all.

A **TLE** (Two-Line Element set) encodes an orbit in two fixed-width 69-column
lines. An **OMM** (Orbit Mean-Elements Message) carries the same orbital
elements as named numeric fields. Both describe the same SGP4 model, and
{func}`~satchecker_client.records.record_elements` derives the same element set
from either, so nothing downstream needs to care which kind a satellite resolved
to.

{func}`~satchecker_client.service.nearest_endpoints_for` picks the endpoint to
ask first from the epoch — the dividing line is
{data}`~satchecker_client.client.HANDOVER_JD` — and always offers the other as a
fallback, because **neither endpoint reports that it has nothing near the epoch
you asked for**. Ask `get-nearest-omm` for a 2021 epoch and it returns its
earliest 2026 record — years off, with nothing in the response to say so. Ask
`get-nearest-tle` for a 2027 epoch and it returns the last TLE ever published.
Only the caller's own staleness policy can tell a good answer from a clamped
one, and when the answer turns out to be unusable, the *other* endpoint is where
the record actually lives. That is also why the handover date is a hint rather
than a cutoff: SatChecker sources OMM from Space-Track as well as Celestrak, and
Space-Track's OMM history runs years deep, so OMM may yet appear for earlier
epochs.

## Fetching records

{func}`~satchecker_client.client.fetch_nearest_tle` and
{func}`~satchecker_client.client.fetch_nearest_omm` each take a NORAD catalogue
number and a UTC Julian Date epoch, and return a pandas `DataFrame` with a
normalised column set ({data}`~satchecker_client.client.TLE_COLUMNS` or
{data}`~satchecker_client.client.OMM_COLUMNS`, plus a `RECORD_KIND` column). An
empty frame means the service answered and has nothing for that satellite.

Failures are typed, and the distinction matters:

- {class}`~satchecker_client.client.SatCheckerError` — the base class.
- {class}`~satchecker_client.client.SatCheckerResponseError` — the service is
  up and answered, but this request failed: a 404 for an unknown catalogue
  number, or a malformed reply. One satellite's problem; the rest of a list is
  still worth asking about.
- {class}`~satchecker_client.client.SatCheckerTransportError` — the service
  could not be reached. Every further request is one you already know is
  unwelcome.
- {class}`~satchecker_client.client.SatCheckerRateLimitError` — a subclass of
  the transport error: the service answered HTTP 429 to say this client should
  back off. Carries the `Retry-After` hint as `retry_after` (seconds) when the
  service supplies one.

### Batches

```python
for label, fetch in sc.nearest_endpoints_for(epoch_jd):
    result = sc.fetch_nearest_batch(norad_ids, epoch_jd, fetch_nearest=fetch, endpoint=label)
    if result.outage is not None:
        raise result.outage          # the service, not this satellite
    for norad_id, err in result.errors.items():
        print(f"{norad_id}: {err}")  # this satellite, keep going
    print(result.records)
```

{func}`~satchecker_client.service.fetch_nearest_batch` is written to be a
considerate client of a free public service:

- Requests are issued at most `max_workers` at a time (default
  {data}`~satchecker_client.service.MAX_WORKERS`, five), submitted one at a time
  as earlier ones land rather than queued all at once.
- A transport failure or a rate limit stops the batch there: no further
  requests are sent, so an outage costs at most `max_workers` requests no matter
  how many satellites were asked for.
- A response failure is recorded per satellite and the rest of the batch
  continues — but should the service reject ten consecutive requests with the
  same status and no success in between, the batch concludes it is facing a
  wall rather than ten absent satellites, and stops there too.
- Every returned record is validated and checked against the NORAD ID it was
  requested for before it is included.

The {class}`~satchecker_client.service.NearestBatchResult` separates these
outcomes: `records` (the validated rows), `errors` (per-satellite failures,
including the IDs never sent after an outage), and `outage` (set when the
service itself was the problem — the signal that retrying against the *other*
endpoint would be asking a down service a different question).

## Asking about a record

Records are pandas rows. Rather than testing for columns yourself, ask:

- {func}`~satchecker_client.records.record_kind` — `"tle"` or `"omm"`.
- {func}`~satchecker_client.records.record_epoch_jd` — the epoch as a UTC
  Julian Date. A TLE's epoch is always re-derived from line 1, never taken from
  a provider field; an OMM's `EPOCH` is parsed and range-checked instead.
- {func}`~satchecker_client.records.record_elements` — the seven shared
  orbital elements plus `BSTAR`, in the same units for either kind.
- {func}`~satchecker_client.records.validate_record` — everything below, in
  one call; returns the satellite's embedded NORAD ID.

### What validation guarantees, per kind

The two formats do not offer the same guarantees, and the difference is worth
being explicit about rather than letting it pass silently.

Both kinds are checked for a present, numeric, finite, whole-number
`NORAD_CAT_ID`; finiteness on all seven orbital elements; and element ranges
(inclination in [0, 180]; RAAN, argument of pericenter and mean anomaly in
[0, 360); eccentricity in [0, 1); mean motion strictly positive).

A **TLE** additionally gets two checks with no OMM equivalent:

- **The modulo-10 checksum** on each 69-column line — what makes
  single-character corruption detectable. A flipped digit inside a fixed-width
  numeric field otherwise parses cleanly, stays in range, and silently shifts
  the modelled trajectory.
- **The embedded identity cross-check.** Both lines carry the satellite
  identifier and must agree with each other and with the row, so a record filed
  under the wrong satellite is caught.

**An OMM record has no checksum**, and there is no way to add one. Its `EPOCH`
must parse as ISO 8601 and fall inside an absolute plausibility window (not
before 1957, not more than a year in the future); that and the range checks are
what stand in for it, and they are weaker. This is a property of the format,
not of the handling.

## Caching

```python
cache = sc.TextOrbitCache("~/.cache/my-app/orbits")
sc.store_or_warn(
    lambda: cache.store(norad_id, result.records),
    cache.path(norad_id),
    "nearest records",
)
known = cache.get(norad_id)
```

{class}`~satchecker_client.cache.TextOrbitCache` keeps one atomically-written,
versioned JSON envelope per satellite (`orbit-<NORAD>.json`). Records are keyed
by their contents and epoch rather than by the request that fetched them, so one
record can serve any number of nearby epochs, and one file holds both kinds —
around the handover a satellite will typically carry its last TLEs and its first
OMM records side by side.

Reads validate the schema version, the NORAD identity, and every field consumed
downstream. An absent file is an ordinary miss; a file that exists but cannot be
used is reported and treated as a miss, so a cache that never takes hold does
not silently cost a request every run. `store` merges and deduplicates rather
than overwrites, and {func}`~satchecker_client.service.store_or_warn` turns a
failed write into a warning instead of losing the fetched records to an I/O
error.

{func}`~satchecker_client.cache.read_legacy_tle_records` reads a directory of
plain pandas-oriented JSON files — the shape of a Space-Track `gp` export —
for callers migrating from files they already have.

## What stays with the caller

This package takes no view on *which* record an application should use. Source
precedence, how stale a record may be before it is refused, whether a stale
answer from the primary archive should trigger a request to the other one, and
whether missing coverage is fatal are application policy. TABASCAL, the
original consumer, documents its policy — nearest-record selection, age
ceilings, cache-reuse thresholds, and complete-coverage enforcement — in its
own [orbit records guide](https://tabascal.readthedocs.io/en/latest/orbits.html).
