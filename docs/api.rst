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
millisecond.

.. autofunction:: satchecker_client.datetime_to_jd

.. autofunction:: satchecker_client.jd_to_datetime

TLE parsing
-----------

.. automodule:: satchecker_client.tle_parse
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
