# satchecker-client

A small Python client for the [IAU CPS SatChecker][satchecker] service, which
publishes nearest-epoch satellite orbital records — TLEs up to the 2026-07-12
archive handover, OMM element sets after it. No account or credentials are
needed.

> **Unofficial.** This is a third-party client written for the
> [TABASCAL][tabascal] and [tab-sim][tabsim] radio-astronomy packages. It is not
> published, endorsed, or maintained by the IAU CPS SatHub team, who develop the
> [SatChecker service itself][upstream]. The distribution name deliberately
> avoids `satchecker` so that name stays available to them.

## What it does

- **Transport** — both nearest-record endpoints, with normalised column sets and
  a typed error hierarchy that separates a per-satellite miss from a service
  outage or a rate limit.
- **Parsing** — TLE line parsing including Alpha-5 catalogue numbers and
  checksums, plus the element range and finiteness checks both record kinds
  share.
- **Kind dispatch** — one place that knows how TLE and OMM records differ, so
  callers spanning the archive handover do not thread a format flag through
  their own code.
- **Caching** — a validated, atomically-written per-NORAD JSON store.
- **Batching** — bounded-concurrency fetches that stop on the first sign the
  service itself is the problem rather than working through the rest of a list.

## What it does not do

It takes no view on *which* record your observation should use. Source
precedence, how stale a record may be before it is refused, and whether missing
coverage is fatal are application policy and stay with the caller.

## Install

```bash
pip install satchecker-client
```

## Use

```python
import satchecker_client as sc

# Identify your application to the service operators. Optional, but the service
# is a courtesy to the community and shared traffic is easier to reason about
# when it is attributable.
sc.set_client_identifier("my-app/1.0")

epoch_jd = 2460800.5
norad_ids = [25544, 48274]

# nearest_endpoints_for picks TLE, OMM, or both, based on where the epoch falls
# relative to the archive handover.
for label, fetch in sc.nearest_endpoints_for(epoch_jd):
    result = sc.fetch_nearest_batch(norad_ids, epoch_jd, fetch_nearest=fetch, endpoint=label)
    if result.outage is not None:
        raise result.outage          # the service, not this satellite
    for norad_id, err in result.errors.items():
        print(f"{norad_id}: {err}")  # this satellite, keep going
    print(result.records)
```

Records are `pandas` rows; ask `record_kind`, `record_epoch_jd` and
`record_elements` about one rather than testing for columns yourself.

Caching a fetch for reuse across nearby epochs:

```python
cache = sc.TextOrbitCache("~/.cache/my-app/orbits")
sc.store_or_warn(cache, norad_id, result.records)
known = cache.get(norad_id)
```

## Development

```bash
pip install -e ".[test]"
pytest
```

The tests block outbound network access via an autouse fixture, so the suite
runs offline and never touches the live service.

## Licence

GPL-3.0-or-later — see [LICENSE](LICENSE). Extracted from
[TABASCAL][tabascal], which carries the same licence.

[satchecker]: https://satchecker.cps.iau.org/
[upstream]: https://github.com/iausathub/satchecker
[tabascal]: https://github.com/epfl-radio-astro/tabascal
[tabsim]: https://github.com/epfl-radio-astro/tab-sim
