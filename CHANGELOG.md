# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Each release is also
published on [GitHub](https://github.com/epfl-radio-astro/satchecker-client/releases)
and [PyPI](https://pypi.org/project/satchecker-client/).

## [Unreleased]

## [0.2.0] - 2026-09-18

Three additive pull requests. The existing fetchers, validators and cache keep
their signatures and defaults, and `tests/test_public_compatibility.py` pins the
pre-0.2.0 exports and call patterns. The Python floor stays at 3.10.

### Added

- `search_satellites(name)`, a supported wrapper for SatChecker's
  `search-satellites` endpoint with the same error contract as the record
  fetchers. Returns one row per catalogue entry (`SEARCH_COLUMNS`); the
  service's matching rules are reported rather than smoothed over: case-sensitive
  `LIKE`, unescaped `%` and `_`, several rows per NORAD ID, several IDs per
  object, decayed objects included.
  ([#3](https://github.com/epfl-radio-astro/satchecker-client/pull/3))
- `catalogue.in_orbit_candidates(found, epoch_jd)`, which shortlists the IDs in
  orbit at a past epoch from search rows, keeping satellites that have since
  re-entered and excluding ones launched later. Search results are cached beside
  the orbit records, one `search-<key>.json` file per query. ([#3])
- `fetch_nearest_tle` / `fetch_nearest_omm(..., strict_response=True)`, which
  raise `SatCheckerResponseError` for a reply that makes no sense (an error
  envelope served with HTTP 200, a missing or null data field, two recognised
  fields that disagree) instead of returning an empty frame that looks like "no
  such record". Transport classification is unchanged.
  ([#4](https://github.com/epfl-radio-astro/satchecker-client/pull/4))
- `records.validated_record(record, *, allow_missing_checksum=False)`, which
  returns the record itself in canonical form: an explicit `RECORD_KIND`, the
  row ID checked against the ID embedded in the lines, the stray backslash
  removed, and a `TLE_CHECKSUM_STATUS` of `verified` or
  `unverified_missing_checksum` that is provenance and is never upgraded. ([#4])
- `cache.read_orbit_file(path)`, a strict reader for one explicitly named orbit
  table with values exactly as written; unreadable input raises
  `CacheValidationError` naming the path. ([#4])
- Public `datetime_to_jd` / `jd_to_datetime`, with their assumptions documented.
  ([#4])
- `satchecker_client.resolve.resolve_orbits(norad_ids, obs_epoch_jd, *, ...)`,
  an optional resolver that chooses, for each requested satellite, the record to
  use at an epoch under rules the caller supplies. Every policy argument is
  required; the client installs no defaults. Outcomes are structured
  (`ResolvedOrbit`, `RejectedOrbit` with the governing limit and the refusing
  error, `service_errors`, `refresh_errors`, an `unavailable` classification,
  per-endpoint `attempts` and `events`), an outage is never read as absence,
  and records the layer cannot vouch for are withheld from the shared cache. A
  record whose elements pass the range checks and then overflow in the
  arithmetic behind them is refused like any other invalid record rather than
  escaping as a bare `OverflowError`.
  ([#5](https://github.com/epfl-radio-astro/satchecker-client/pull/5))
- `read_extra_orbit_dir(directory)`, strict ingestion of a directory of orbit
  tables; errors are `OrbitInputError` carrying the path, row and a `code`
  (`unreadable`, `structure`, `identity`, `invalid_record`, `checksum_policy`).
  ([#5])
- `satchecker_client.replay`: `save_orbits_for_reuse`, `save_replay_orbits` and
  `load_replay_orbits` make the `used_orbits.json` + `norad_ids.yaml` pair one
  format. Existing tables stay readable, and new tables stay readable by
  `read_legacy_tle_records`. ([#5])

### Changed

- SatChecker's records backfilled from Space-Track in May 2025 carry damaged
  TLE lines for epochs between roughly 2001 and 2018. A line with a stray
  backslash after its checksum digit (78% of the damaged sample) is now repaired
  and verified as usual instead of being rejected, so historical epochs resolve
  where 0.1.x found nothing; repaired lines are written to the cache in standard
  form. ([#3])
- A line with no checksum digit is still rejected by default everywhere,
  `fetch_nearest_batch` included, which gains an `allow_missing_checksum`
  keyword (default `False`). Opting in accepts such lines with one warning per
  batch, and the records are never written to the shared cache. ([#3])

### Fixed

- Malformed input to `cache.store` raises `CacheValidationError` instead of
  escaping as an `AttributeError` or `KeyError`. ([#3])

## [0.1.2] - 2026-09-16

### Fixed

- Replay files read by `read_legacy_tle_records` decode floats to the exact
  double that was written. Previously ordinary orbital elements came back as
  neighbouring doubles (an eccentricity of `0.0066635` as
  `0.006663499999999999`), so a replayed trajectory could differ from the run
  it reproduces.
  ([#2](https://github.com/epfl-radio-astro/satchecker-client/pull/2))

### Changed

- A replay file containing a subnormal value (below `2.2250738585072014e-308`),
  in any column, is skipped as unreadable. No orbital element comes near that
  range. ([#2])

## [0.1.1] - 2026-08-26

Fixes for findings from an external review of the package.
([#1](https://github.com/epfl-radio-astro/satchecker-client/pull/1))

### Fixed

- Concurrent cache stores serialise on a per-file lock (`flock`, with an
  `msvcrt` fallback on Windows), so two read/merge/write transactions can no
  longer discard each other's records.
- A blank TLE eccentricity field no longer reads as a circular orbit; the field
  must be exactly seven ASCII digits. A blank BSTAR still means zero.
- The public fetch functions no longer return records for a different
  satellite: stray rows are dropped, and a response carrying only other
  satellites' records raises `SatCheckerResponseError`.
- NORAD IDs validate exactly and must be positive; `norad_id_of` no longer
  converts through `float`, which accepted `0` and `-1` and rounded IDs above
  2^53.
- `TextOrbitCache` expands `~` in its directory, and the caching example in the
  README and usage guide calls `store_or_warn` with its actual signature.

## [0.1.0] - 2026-08-24

First release of the standalone SatChecker client, extracted from
[TABASCAL](https://github.com/epfl-radio-astro/tabascal) and relicensed to MIT.

### Added

- Transport for both nearest-record endpoints (TLE and OMM) with normalised
  columns and a typed error hierarchy separating per-satellite misses from
  service outages and rate limits.
- TLE line parsing (Alpha-5 catalogue numbers, checksums) and the shared
  element range and finiteness checks.
- Kind dispatch across the 2026-07-12 TLE-to-OMM archive handover.
- Validated, atomically written per-NORAD JSON caching.
- Bounded-concurrency batch fetches that stop on the first sign of a
  service-level problem.
- Python 3.10 to 3.14; depends only on pandas and numpy.

[Unreleased]: https://github.com/epfl-radio-astro/satchecker-client/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/epfl-radio-astro/satchecker-client/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/epfl-radio-astro/satchecker-client/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/epfl-radio-astro/satchecker-client/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/epfl-radio-astro/satchecker-client/releases/tag/v0.1.0
[#1]: https://github.com/epfl-radio-astro/satchecker-client/pull/1
[#2]: https://github.com/epfl-radio-astro/satchecker-client/pull/2
[#3]: https://github.com/epfl-radio-astro/satchecker-client/pull/3
[#4]: https://github.com/epfl-radio-astro/satchecker-client/pull/4
[#5]: https://github.com/epfl-radio-astro/satchecker-client/pull/5
