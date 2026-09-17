API Reference
=============

The package is split by responsibility; everything public is re-exported at the
top level, so ``import satchecker_client as sc`` is the only import most
callers need.

Package
-------

.. automodule:: satchecker_client

Client
------

Transport and response normalisation. Both nearest-record functions take an
opt-in keyword-only ``strict_response``, which changes only which replies are
refused; the default reading of a reply is unchanged.

.. automodule:: satchecker_client.client
    :members:

Catalogue
---------

.. automodule:: satchecker_client.catalogue
    :members:

Service
-------

.. automodule:: satchecker_client.service
    :members:

Records
-------

What a record is and whether it is usable. :func:`~satchecker_client.records.validated_record`
additionally returns the record itself, canonical and with its checksum
provenance stated.

.. automodule:: satchecker_client.records
    :members:

Time
----

The UTC Julian-date conversions every epoch in this package is stated in. A
caller that keeps records or compares their epochs against its own clock uses
these rather than a second convention that agrees to the second and not the
millisecond. What they assume:

- A naive ``datetime`` is read as UTC; an aware one is converted to UTC first,
  so ``2023-02-24 14:00+02:00`` and ``2023-02-24 12:00`` both give ``2460000.0``.
- :func:`~satchecker_client.jd_to_datetime` returns a naive UTC ``datetime``.
- Days are 86 400 seconds; leap seconds are not modelled, which is also how
  TLE and OMM epochs are written.
- A Julian Date is a double, and near 2.46e6 adjacent doubles are about 40 µs
  apart, so timestamps a few microseconds apart can map to one value and a
  round trip is exact to well under a millisecond, not to the microsecond.

.. autofunction:: satchecker_client.datetime_to_jd

.. autofunction:: satchecker_client.jd_to_datetime

TLE parsing
-----------

.. automodule:: satchecker_client.tle_parse
    :members:

Resolve
-------

Optional, and the only part of the package that decides *which* record an
observation gets — under rules the caller states, every one of them a required
keyword with no default. :func:`~satchecker_client.resolve.resolve_orbits`
returns an :class:`~satchecker_client.resolve.OrbitResolution`: accepted records
with the source and signed offset that chose them
(:class:`~satchecker_client.resolve.ResolvedOrbit`), the nearest near-miss with
the parameter that refused it — or, for an unusable record, the exception that
did (:class:`~satchecker_client.resolve.RejectedOrbit`), one
:class:`~satchecker_client.resolve.EndpointAttempt` per configured endpoint for
every satellite that entered the remote group, the request failures, the
satellites nothing was found for and why, and a
:class:`~satchecker_client.resolve.ResolutionEvent` for each decision. Of its
own it raises only for a malformed rule or identity — a satellite it could not
resolve is evidence in the result, not an exception — though an ``on_event``
callback's own exception propagates through it;
:class:`~satchecker_client.resolve.OrbitInputError` comes from the strict
directory reader beside it, and from the replay loader. Its ``code`` is one of
the ``INPUT_*`` categories below, so a caller can tell a file it could not read
from a record its own checksum policy refused without matching the message.

.. automodule:: satchecker_client.resolve
    :members:

Replay
------

Writing the orbital inputs a run used, and reading them back exactly. The two
files are read as a pair and as nothing else: no directory scan, no cache, no
request, no reselection by age.

.. automodule:: satchecker_client.replay
    :members:

Cache
-----

Per-satellite record storage and catalogue-search snapshots.
:func:`~satchecker_client.cache.read_orbit_file` reads one explicitly named
orbit table and raises, rather than returning an empty frame, for input it
cannot read — a table that is genuinely empty still reads as an empty frame;
:func:`~satchecker_client.cache.read_legacy_tle_records` scans a directory and
skips what it cannot use.

.. automodule:: satchecker_client.cache
    :members:
