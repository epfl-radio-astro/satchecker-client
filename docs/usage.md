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

## Choosing which record to use

Everything above answers one question at a time. Choosing the record an
observation gets means holding several answers at once — a file you supplied
against the cache against two archives, an age ceiling that says what is
acceptable against a reuse threshold that says what is worth a request, and a
failed request that must never read as a satellite the archives do not have.
{func}`~satchecker_client.resolve.resolve_orbits` is that machinery, and it is
optional: nothing else in the package goes through it.

It has no policy of its own. Every selection and acquisition rule is a required
keyword with no default, because a default here would be one application's
policy installed in a library every other one also installs:

```python
import satchecker_client as sc

epoch_jd = 2460800.5
cache = sc.TextOrbitCache("~/.cache/my-app/orbits")

resolution = sc.resolve_orbits(
    [25544, 48274],
    epoch_jd,
    source_order=("extra", "remote"),      # your files first, then cache+service
    remote_max_age_days=3.0,               # hard ceiling on a remote record
    cache_reuse_max_age_days=1.0,          # closer than this, do not ask again
    extra_orbit_max_age_days=None,         # your records are yours: no ceiling
    replacement="strictly_fresher",        # only a closer record displaces one held
    offline=False,
    allow_missing_checksum=False,
    strict_response=True,
    endpoints=sc.nearest_endpoints_for(epoch_jd),
    fallback=True,                         # try the other archive when needed
    max_workers=sc.MAX_WORKERS,
    cache=cache,                           # or None for no cache at all
    extra_records=sc.read_extra_orbit_dir("my_orbits/"),
)

frame = resolution.frame()   # one row per accepted satellite, in requested order
```

Those ages, the source order and the replacement rule are the two consuming
applications' current ones; `strict_response` is not, and is shown here at its
stricter setting — tab-sim asks for it, tabascal currently takes the lenient
reading the endpoints have always had. None of these are recommendations and
none are defaults; pick your own and state them.

Each requested ID is resolved independently, through `source_order` in turn:
`"extra"` is the frame you passed as `extra_records`, `"remote"` is the cache and
the service together. The first group that produces an acceptable record for an
ID wins, and the later ones are not consulted *for that ID* — so an acceptable
record of your own means the service is never asked about that satellite. Within
a group the nearest record to the observation epoch wins, measured from the
record's own epoch: a TLE's line 1, an OMM's checked `EPOCH`, never a provider's
`epoch` field or a saved `EPOCH_JD` beside it.

### What comes back

{class}`~satchecker_client.resolve.OrbitResolution` is evidence, not a verdict —
it raises nothing for a satellite it could not resolve, and
`resolution.missing` is in requested order:

```python
for norad_id in resolution.missing:
    if norad_id in resolution.service_errors:
        raise resolution.service_errors[norad_id]      # we could not find out
    rejected = resolution.rejected.get(norad_id)
    if rejected is not None and rejected.reason_code == "over_age":
        print(f"{norad_id}: nearest record was {rejected.age_days:.2f} d away, "
              f"over {rejected.limit_name}={rejected.ceiling_days}")
    elif rejected is not None:
        print(f"{norad_id}: the record found for it was unusable "
              f"({rejected.reason_code})")
    if norad_id in resolution.unavailable:
        print(f"{norad_id}: {resolution.unavailable[norad_id]}")
```

A rejection that is not an age rejection has no epoch to report: `age_days`,
`offset_days`, `ceiling_days` and `limit_name` are all `None` on it, because
nothing about the record could be measured. What it carries instead is `error`,
the exception that refused *that* candidate, so the diagnostic is at hand
without re-reading the events. It is `None` on an age rejection: nothing
refused that record, it was read, measured and found too far away. Only one
rejection per satellite is kept and it is the first one, so for a satellite with
two unusable candidates the last `candidate_rejected` event describes the other
candidate; `error` describes this one. And the two maps are not
alternatives — an ID can appear in neither, either, or both — so each is asked
about separately.

Four outcomes are kept apart, because flattening them is how a service failure
becomes a plausible-looking run with a satellite quietly missing from it:

| Outcome | Where it is | What it means |
|---|---|---|
| Accepted | `resolved[id]` — a {class}`~satchecker_client.resolve.ResolvedOrbit` | The record, its `source` (`extra`, `cache`, `service`), the `endpoint` that answered, and the signed `offset_days` that chose it |
| Refused on age | `rejected[id]` — a {class}`~satchecker_client.resolve.RejectedOrbit` | A record was found and judged; `reason_code`, `ceiling_days` and the `limit_name` that refused it |
| Request failed | `service_errors[id]` | The service was asked and did not answer usably. Not a record, and not an absence |
| Nothing found | `unavailable[id]` | `absent` only when every endpoint the fallback policy needed answered successfully with nothing; otherwise `offline`, `not_attempted` or `invalid_local` |

An ID refused on age, one with a service failure, and one whose fallback an
outage prevented are deliberately *not* in `unavailable`: each already has its
own evidence, and calling any of them absent would report a satellite the
archives do have as one they do not. Two classifications do sit alongside an age
rejection, because each says something the rejection does not: `offline` — the
ceiling refused the record that was held, and nothing was allowed to look for a
closer one — and `not_attempted`, when `source_order` named no remote group, so
nothing was ever going to look. `refresh_errors` holds the failures of IDs that stayed resolved
anyway — never fatal, since the run has a record, but the run is not quite the
one that was asked for and this is the only place that says so.

`attempts[id]` lists one
{class}`~satchecker_client.resolve.EndpointAttempt` per configured endpoint, in
order, with `not_sent` for one that was never asked — a cache hit is therefore
a row of `not_sent`s. An ID that never reached the remote group at all, such as
one an earlier source group resolved, has no `attempts` entry: nothing was
decided about asking. `events` is the same facts as they happened —
{class}`~satchecker_client.resolve.ResolutionEvent`, each with a stable `code`,
the IDs it concerns, and the source, endpoint, path or error behind it. Pass
`on_event=` to receive them as they occur, from the thread that called
`resolve_orbits` and never from a worker; an exception your callback raises is
yours and propagates out of the call. The result keeps the events either way, so
an application that installs no callback is not reading a different run. The
codes are `candidate_rejected`, `source_selected`,
`cache_hit`, `refresh_required`, `refresh_skipped`, `batch_started`,
`endpoint_fallback`, `outage`, `incumbent_retained`, `refresh_failed`,
`unverified_accepted`, `unverified_not_cached` and `cache_write_failed`. The
wording around them is yours; `log=` remains the low-level diagnostic stream the
cache, the batch and the cache writes already write to.

`resolution.frame()` derives the orbital elements here, from each accepted
record, overwriting any element columns a legacy file arrived with rather than
duplicating them; `records()` hands back independent copies, and neither touches
the frame you passed in.

### Reuse is not acceptance

The two cache settings answer different questions, and collapsing them is a
quiet way to lose a satellite. `remote_max_age_days` is what an acceptable
record is; `cache_reuse_max_age_days` is when a record already held is close
enough that a request is not worth making. A cached record only suppresses the
request if it satisfies *both* — otherwise `cache_reuse_max_age_days=None` would
make every cached record a hit, including ones the hard ceiling then refuses, so
the satellite would never be fetched at all. A reuse threshold above the hard
ceiling is refused outright for the same reason.

An acceptable-but-stale cached record stays in place while the service is asked
for something closer. That is what `replacement` decides:
`"strictly_fresher"` keeps it unless the answer is strictly closer, which makes
a refresh safe by construction — a staler, equally distant or failed answer
leaves the run with what it already had. `"prefer_service"` takes any in-ceiling
answer instead. Neither relaxes the ceiling.

### Fallback is not a search across both archives

Neither endpoint says when it has nothing near the epoch you asked for, so
`fallback=True` gives an ID one more request when the previous endpoint supplied
no acceptable in-ceiling record — an empty answer, an over-age one, or a failed
one. What it does *not* do is ask the other archive because the record already
held is closer: an in-ceiling answer is an answer, and asking anyway would make
this a global-nearest search across both archives, which is a different
acquisition policy and a different number of requests to a free public service.

An outage stops acquisition entirely, whatever `fallback` says, and the IDs whose
fallback it prevented stay unknown rather than absent — including ones that had
already answered empty at an earlier endpoint.

### Offline

`offline=True` forbids every request without relaxing any ceiling: offline is
about what can be reached, not about what an acceptable record is. A cached
record outside `remote_max_age_days` is refused exactly as it would be online,
and its ID is reported as `offline` rather than absent, because nothing asked.

### Your own files: strict or forgiving

The resolver never opens a directory. You read your records and pass the frame,
which keeps the ingestion contract yours:

- {func}`~satchecker_client.resolve.read_extra_orbit_dir` reads every `*.json`
  in a directory through {func}`~satchecker_client.cache.read_orbit_file` and
  raises {class}`~satchecker_client.resolve.OrbitInputError` — naming the file,
  and the row for a row-level failure — for anything it cannot read, including a
  table that is not an orbit table and a row whose `NORAD_CAT_ID` is unusable.
  Every row's identity is checked, including rows you did not ask for: a
  malformed identity that survives to a wanted-ID filter simply vanishes from
  it, and the service then answers for the satellite the file was meant to
  supply. A missing path is an empty frame; whether that deserves a warning is
  yours to judge, since you know whether the path came from a default or from a
  user.
- {func}`~satchecker_client.cache.read_legacy_tle_records` is the forgiving
  scan, unchanged: it skips what it cannot use and returns the rest.

Either frame is acceptable as `extra_records`. Reading is not accepting: neither
reader applies a checksum policy, and the resolver applies
`allow_missing_checksum` identically to a file, the cache and the service —
a default that rejected an unverifiable line remotely and accepted it from a
file would advertise a strictness whose workaround is to save the record once.
Accepted records carry their provenance, which never improves; records nothing
has verified are used by the run and kept out of the shared cache, which every
application reading it also reads.

### Freezing what a run used

```python
ids_path, records_path = sc.save_replay_orbits(
    "run_42/input_data", resolution.norad_ids(), resolution.records()
)

norad_ids, records = sc.load_replay_orbits(
    "run_42/input_data", allow_missing_checksum=False
)
```

{func}`~satchecker_client.replay.save_replay_orbits` writes
{data}`~satchecker_client.replay.REPLAY_IDS_FILE` and
{data}`~satchecker_client.replay.REPLAY_RECORDS_FILE`, and
{func}`~satchecker_client.replay.load_replay_orbits` reads those two files and
*nothing else* — no directory scan, no cache, no request, and no reselection by
age. Saved records are used however far their epochs are from the observation,
because that is what makes them the same records. Anything short of exact stops:
a record for a satellite the ID file does not list, a listed satellite with no
record, two records for one, an identity that does not read. The two files are
validated and serialised before either is opened, so a failure does not leave a
half-written pair, though writing them one after the other is not a transaction.

{func}`~satchecker_client.replay.save_orbits_for_reuse` writes just the table,
to a path you name, and permits several rows for one satellite — a run may use
one twice. The frozen pair does not: matching one saved record to each saved ID
would otherwise be a choice, which is the reselection a replay exists to
prevent.

The table is an ordinary explicit orbit table — the shape both
{func}`~satchecker_client.cache.read_orbit_file` and the forgiving directory
scan read, never a managed cache envelope. It carries what reads back as the
same record: a TLE's two lines and its checksum provenance, an OMM's epoch and
its seven elements, plus whatever provider and fetch metadata the record
arrived with. `EPOCH_JD` and `SEMIMAJOR_AXIS` are not written, since they are
recomputed on every read and a second copy on disk is one a later edit can
silently contradict.

What is exact, and what is not:

- Numbers are written through the standard library's JSON encoder, which writes
  a float as `repr` does — the shortest text that reads back as the same double,
  `-0.0` and subnormals included. `DataFrame.to_json` does not: at its default
  precision it rounds an element outright, and even at its maximum it writes
  `0.0066635` as `0.006663499999999999`, a different double and so a different
  trajectory.
- `allow_missing_checksum` is required on every load, because provenance
  survives the save: a record accepted without its checksum digits needs the
  opt-in again, whatever its lines carry now. A status this package does not
  recognise, and a checksum that is present and wrong, are refused either way.
- A table written by something that formatted its floats — an older consumer's
  `used_orbits_*.json`, say — reads back fine, but the precision it lost before
  it was saved is gone and nothing here invents it.
- What is frozen is the orbital *input*. Reproducing a previous run's
  trajectories also assumes the same observation, the same other inputs and the
  same numerical environment, none of which this package sees.

## What stays with the caller

This package still takes no view on *which* record an application should use.
{func}`~satchecker_client.resolve.resolve_orbits` executes the rules it is
given and has none of its own: source precedence, the age ceilings, the reuse
threshold, whether to fall back to the other archive, and whether missing
coverage is fatal are all the caller's, and so is every sentence a user reads
about them. TABASCAL, the original consumer, documents its policy —
nearest-record selection, age ceilings, cache-reuse thresholds, and
complete-coverage enforcement — in its own
[orbit records guide](https://tabascal.readthedocs.io/en/latest/orbits.html).
