# satchecker-client

A small Python client for the [IAU CPS SatChecker](https://satchecker.cps.iau.org/)
service, which publishes nearest-epoch satellite orbital records — TLEs up to the
2026-07-12 archive handover, OMM element sets after it. No account or credentials
are needed.

```{note}
**Unofficial.** This is a third-party client written for the
[TABASCAL](https://github.com/epfl-radio-astro/tabascal) and
[tab-sim](https://github.com/epfl-radio-astro/tab-sim) radio-astronomy packages.
It is not published, endorsed, or maintained by the IAU CPS SatHub team, who
develop the [SatChecker service itself](https://github.com/iausathub/satchecker).
The distribution name deliberately avoids `satchecker` so that name stays
available to them.
```

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
coverage is fatal are application policy and stay with the caller. TABASCAL, the
original consumer, documents its policy in its own
[orbit records guide](https://tabascal.readthedocs.io/en/latest/orbits.html).

## Install

```bash
pip install satchecker-client
```

Pure Python, depending only on pandas and numpy; Python 3.10–3.14.

```{toctree}
:maxdepth: 2
:caption: Contents

usage
api
readthedocs
```

## Indices

- {ref}`genindex`
- {ref}`modindex`
- {ref}`search`
