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

### Telling absence from an outage

Both fetch functions read a reply leniently by default, and will go on doing so.
The default parser ignores an `error` field, reads the first envelope of a list
and ignores the rest, and takes the first data field holding anything. So a
reply that makes no sense comes back as whatever it appears to hold: an error
envelope served with HTTP 200, a missing data field, a null one and a dropped
second envelope usually leave an empty frame — the same answer as a satellite
the archive genuinely has no record of — while an error envelope that *also*
carries rows, or a list whose first envelope does, returns those rows with
nothing said about the error or the envelopes dropped beside them. For a caller
that uses whatever records it can get, that is harmless. For one where a missing
satellite changes the result rather than shortening the list, it is the failure
to avoid:

```python
frame = sc.fetch_nearest_tle(norad_id, epoch_jd, strict_response=True)
```

`strict_response=True` raises
{class}`~satchecker_client.client.SatCheckerResponseError` for each of those
replies instead, naming the endpoint that answered and carrying the service's
own error text when there is one. It changes nothing else: a reply that never
arrived is classified exactly as it was — one satellite's problem, an outage, or
a rate limit — because that is about the request rather than the body. The
documented ways of saying "no record for this satellite" — an empty top-level
list, an empty `orbital_data`, the legacy `tle_data` spelling — still return an
empty frame, and a good reply normalises to the same values it does by default.
Data fields are selected by presence rather than by truthiness, which is what
makes a null one distinguishable from an absent one, and a reply carrying two
recognised fields that disagree is refused rather than resolved by precedence.

The option is per call and the default is deliberately untouched: a consumer
that does not ask resolves exactly what it always did.

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

## Finding satellites by name

{func}`~satchecker_client.client.search_satellites` turns a name into catalogue
entries, for callers that select satellites by name rather than by number:

```python
found = sc.search_satellites("NAVSTAR")
found[["NORAD_CAT_ID", "OBJECT_NAME", "LAUNCH_DATE", "DECAY_DATE"]]
```

It returns one row per catalogue entry the service matched, with
{data}`~satchecker_client.client.SEARCH_COLUMNS`, and an empty frame when nothing
matches. It is one request however many satellites match; it does not fetch any
orbital records.

What a match means is SatChecker's, and several parts of it are easy to get
wrong:

- The match is a **case-sensitive substring**. Catalogue names are almost all
  upper case, so `"navstar"` finds nothing and `"NAVSTAR"` finds the 80
  entries named `NAVSTAR …`. A few names are mixed case, such as `DMSat-1`, so upper-casing a
  query is a good default rather than a complete one.
- `%` and `_` are **SQL wildcards**, and cannot be escaped.
- **Rows are not satellites.** A satellite known by several names has a row for
  each, and those rows may not carry the same fields — one can have a launch
  date its alias lacks. Combine a satellite's rows before deciding anything
  about it.
- **NORAD IDs are not objects either.** The same object can be listed under two
  catalogue numbers, with the same name and `OBJECT_ID`, and nothing in the
  response says which one is current. Selecting both means counting it twice.
- **Decayed objects are included**, with a `DECAY_DATE`. Whether a satellite
  that has since re-entered belongs in a result depends on the epoch you are
  modelling, which is the caller's to judge.

An empty name raises `ValueError` before any request is made: SatChecker reads
it as no filter and would return the whole catalogue. A reply that does not have
the expected shape — no `data` rows, or a `count` that disagrees with them —
raises {class}`~satchecker_client.client.SatCheckerResponseError` rather than
passing for a search that matched nothing.

### At a past epoch

Selecting satellites by name for a date in the past means keeping the ones that
were in orbit *then* — including satellites that have since re-entered — and
excluding the ones launched later. The catalogue alone cannot settle that, but
it can narrow the list before any record is fetched:

```python
found = sc.search_satellites("MOLNIYA")
candidates = sc.in_orbit_candidates(found, epoch_jd)   # one row per NORAD ID

for label, fetch in sc.nearest_endpoints_for(epoch_jd):
    result = sc.fetch_nearest_batch(
        candidates["NORAD_CAT_ID"].tolist(), epoch_jd, fetch_nearest=fetch, endpoint=label
    )
    ...  # accept only records whose epoch is close enough to epoch_jd
```

{func}`~satchecker_client.catalogue.in_orbit_candidates` combines each
satellite's rows, keeps the earliest launch date and the latest decay date any of
them gives, and drops only the satellites that window excludes. A satellite
with no dates is kept.

What it returns is a shortlist. Debris carries its parent's launch date, so it is
a candidate before it existed, and an object listed under two NORAD IDs is a
candidate twice. Checking each record's age against the epoch you asked for —
which you must do regardless, since neither record endpoint says when it has
nothing near that epoch — removes much of that: debris asked about long before
it was created, and a superseded catalogue number whose records stopped months
earlier. It does not remove all of it. Debris first tracked within your age limit
after the epoch still passes, and so can both of an object's numbers while a
reassignment is recent; what to do about those is yours to decide.

For Molniya at 2019-06-01, the search returned 170 catalogue entries and 38 were
candidates. 34 had a record within three days, three of them satellites that
decayed later, between 2019 and 2024.

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

**SatChecker's historical TLE archive is an exception.** Its records backfilled
from Space-Track in May 2025 carry one of two defects. In a sample of three
long-lived satellites on two dates a year, all six records were damaged in 2003
and in every year from 2005 to 2016, and some in 2001–2002, 2004 and 2017–2018;
that is a sample, not a survey of the archive.

- **A stray backslash after line 1's last column.** It is always removed, and
  the checksum is then verified as usual, so such a record is as trustworthy as
  a clean one.
- **No checksum digit** on one or both lines. Validation rejects such a line
  unless the caller passes `allow_missing_checksum=True`, and then accepts it
  only if every separator and decimal-point column is where the format puts it.
  That catches a character dropped from most of the line, but not from line 2's
  mean motion digits or revolution number, where a deletion moves no anchored
  column: a line missing a mean motion digit validates, with a different mean
  motion. Nothing verifies such a line's digits.

Lines without checksums are rejected everywhere unless the caller asks,
{func}`~satchecker_client.service.fetch_nearest_batch` included. An application
that needs the dates this archive covers can pass `allow_missing_checksum=True`
to the batch; it should then decide what that means for the records it keeps,
since {func}`~satchecker_client.records.validate_record` will reject the same
lines again when it reads a saved copy, unless it too is asked not to. The batch
returns records with their lines in standard form and logs one warning per batch
naming the satellites, separately for the repaired and the unverifiable. Records
with no checksum digit are never written to the cache, which other applications
and older versions of this package also read, so they are fetched again on each
run. A direct call to {func}`~satchecker_client.client.fetch_nearest_tle`
reports the lines as the service sent them.

**An OMM record has no checksum**, and there is no way to add one. Its `EPOCH`
must parse as ISO 8601 and fall inside an absolute plausibility window (not
before 1957, not more than a year in the future); that and the range checks are
what stand in for it, and they are weaker. This is a property of the format,
not of the handling.

### Keeping exactly what you used

{func}`~satchecker_client.records.validated_record` runs those same checks and
hands back the record rather than an ID — a copy, in canonical form, with its
assurance stated:

```python
kept = sc.validated_record(row, allow_missing_checksum=True)
kept["TLE_CHECKSUM_STATUS"]        # 'verified' or 'unverified_missing_checksum'
```

It is for a caller that has to keep, save and later reread the record it
actually propagated. The returned `dict` carries an explicit `RECORD_KIND`, a
`NORAD_CAT_ID` checked against the identity embedded in a TLE's lines, those
lines in standard form with a stray backslash removed, every other field as it
came — provider metadata included — and, for a TLE,
{data}`~satchecker_client.records.CHECKSUM_STATUS_FIELD`. A pandas row is as
acceptable as a mapping.

The status is provenance, and it never improves. A record accepted without
checksum digits stays `unverified_missing_checksum` through a repair, a save and
a reload, and one already marked so stays marked however well its current lines
checksum — nothing verifies the digits its source omitted. Such a record
therefore needs `allow_missing_checksum=True` on every pass rather than only the
first; without that, one permissive run would launder a record into every strict
run after it. A status this package does not recognise is refused rather than
ignored, and a *corrupt* checksum is refused under either policy — allowing
missing checksums must not weaken what a present one means. An OMM record gets no
status at all: it has no checksum to make a claim about.

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
for callers migrating from files they already have. Floats in those files read
back as the exact doubles that were written; its reference entry notes the two
cases where pandas still differs.

### Reading one file you named

{func}`~satchecker_client.cache.read_orbit_file` reads a single orbit table, and
refuses rather than salvages:

```python
records = sc.read_orbit_file("previous_run/used_orbits.json")
```

The directory scan above skips a file it cannot use and returns what it could
read, which is what a directory of assorted exports needs. A file the caller
named by hand is the opposite case: an empty frame would let an unreadable file
fall through to another source, or to none, and the run would then look like one
where the satellite simply had no record. So malformed content — bytes that are
not even text, or a member name repeated within one JSON object, included — raises
{class}`~satchecker_client.cache.CacheValidationError` naming the path, and a
missing or unreadable file raises `OSError`. A supported but empty table is not
an error — it says a completed run selected nothing, which a missing file does
not say.

Two shapes are tables: a top-level list of record objects, and the
`{column: {index: value}}` orientation `DataFrame.to_json()` writes by default.
Rows come back positionally indexed, whichever was used, and two columns listing
the same rows in a different key order are one table. Nothing else is guessed
at — a dict of lists is refused, and so is a managed `orbit-<NORAD>.json`
envelope, which carries a schema version and a satellite identity that only
{meth}`~satchecker_client.cache.TextOrbitCache.get` checks.

Decoding is the standard library's, so every column survives, provenance such as
`FETCHED_AT` and `TLE_CHECKSUM_STATUS` included, and every number is what was
written: an integer token stays an exact Python integer (up to Python's
configured integer-conversion limit), and a decimal or exponent token is the
correctly rounded double — subnormals and `-0.0` with it. Columns are `object`, holding
those values as decoded: inferring one type per column is what turns an integer
beside a null into a float, which a nanosecond timestamp does not survive. JSON's
non-standard `NaN`, `Infinity` and `-Infinity` literals are refused, as is a
decimal or exponent token with no double, such as `1e400`: no orbital element may be any of
them. No acceptance policy is applied and no record is selected, so an
unverifiable TLE line comes back byte for byte, for
{func}`~satchecker_client.records.validated_record` to accept or refuse.

### Search results

The same cache keeps catalogue searches, one `search-<key>.json` file per query
in the same directory as the orbit files, so a name-selected run can resolve its
satellites without the network once both are cached:

```python
from datetime import datetime, timedelta, timezone

name = "NAVSTAR"
snapshot = cache.get_search(name)
too_old = snapshot is None or datetime.now(timezone.utc) - snapshot.fetched_at > timedelta(days=7)
if too_old:
    try:
        found = sc.search_satellites(name)
        sc.store_or_warn(
            lambda: cache.store_search(name, found), cache.search_path(name), "search result"
        )
    except sc.SatCheckerTransportError as error:
        if snapshot is None:
            raise
        print(
            f"warning: could not refresh the search for {name!r} ({error}); using "
            f"the result fetched {snapshot.fetched_at:%Y-%m-%d %H:%M} UTC, "
            f"{len(snapshot.found)} rows"
        )
        found = snapshot.found
else:
    found = snapshot.found
```

Say so whenever you fall back like this. An out-of-date result can include a
satellite that has since been ruled out or miss one added since, and a cached
empty result selects nothing at all; the warning is how whoever runs it finds
out.

What makes this different from the orbit records shapes how the files behave:

- **The key is the name exactly as sent.** The service matches case-sensitively
  and reads `%` and `_` as wildcards, so `NAVSTAR` and `navstar` are cached
  separately, as they are searched separately.
- **A file holds one complete result, replaced on every store.** Orbit records
  never change once published, so they merge; a search result is a snapshot of
  a catalogue that does change, and rows from an older search say nothing about
  a newer one.
- **A search that matched nothing is cached**, as an empty `found`, and is not
  a miss.
- **Nothing judges age.** {meth}`~satchecker_client.cache.TextOrbitCache.get_search`
  returns `fetched_at` and leaves the decision to you. Staleness errs both ways:
  an old result can lack a decay date added since, keeping a satellite that
  should now be excluded, and it can lack a satellite added to the catalogue, or
  given a matching alias, since it was fetched. For a run you need to reproduce
  exactly, keep the NORAD IDs it actually used rather than relying on a cached
  search.

The service is part of each search's key: the cache reads
{data}`~satchecker_client.client.BASE_URL` as the client does, so results from
a client pointed at another service are cached apart from the default service's.
Each cache method reads it once, but nothing ties a result to the service it
came from. **Point the client at another service before a lookup, fetch, store
and fallback sequence, not during one**: a result fetched from one service and
stored after the address changes is filed, and served back, as the other's.

Search files use their own schema version and are only ever opened by name, so
versions of this package that predate them, reading the same directory, never
see them.

## What stays with the caller

This package takes no view on *which* record an application should use. Source
precedence, how stale a record may be before it is refused, whether a stale
answer from the primary archive should trigger a request to the other one, and
whether missing coverage is fatal are application policy. TABASCAL, the
original consumer, documents its policy — nearest-record selection, age
ceilings, cache-reuse thresholds, and complete-coverage enforcement — in its
own [orbit records guide](https://tabascal.readthedocs.io/en/latest/orbits.html).
