"""Guards on everything that existed before the optional resolver was added.

Both consumers depend on this package with an unbounded requirement, so the old
surface is not "the previous version" — it is the version an installed
application will pick up. Nothing here may change: not a name, not a parameter,
not a default, not a cache schema, and not the behaviour of a call tabascal
makes today.

The signature table is recorded from ``06dcbf5`` deliberately rather than
derived from the code under test: a table computed from the same module it is
checking agrees with any change whatsoever. New names may be added; none of
these may move.
"""

import importlib
import inspect
import json
import sys
import urllib.parse

import pandas as pd
import pytest

import satchecker_client as sc
from satchecker_client import cache as cache_module
from satchecker_client import (
    catalogue,
    client,
    records,
    service,
    tle_parse,
    _time,
    _version,
)
from satchecker_client.cache import (
    SCHEMA_VERSION,
    SEARCH_SCHEMA_VERSION,
    TextOrbitCache,
    read_legacy_tle_records,
)
from satchecker_client.records import CHECKSUM_STATUS_FIELD, validate_record
from satchecker_client.service import (
    fetch_nearest_batch,
    nearest_endpoints_for,
    store_or_warn,
)

from .tle_helpers import (  # noqa: F401  block_network is an autouse fixture
    block_network,
    jd,
    make_nearest_json,
    make_tle,
    write_legacy_tle_file,
)
from .resolve_helpers import forbid, replay_module, resolve_module


OBS = jd(2023, 2, 21)

A, B = 25544, 38833


#: The package exports as of ``06dcbf5``. Additions are allowed; removals and
#: relocations are not.
EXPORTS_AT_06DCBF5 = (
    "datetime_to_jd",
    "jd_to_datetime",
    "__version__",
    "BASE_URL",
    "HANDOVER_JD",
    "OMM_COLUMNS",
    "SEARCH_COLUMNS",
    "TLE_COLUMNS",
    "fetch_nearest_omm",
    "nearest_endpoints_for",
    "SatCheckerError",
    "SatCheckerRateLimitError",
    "SatCheckerResponseError",
    "SatCheckerTransportError",
    "fetch_nearest_tle",
    "search_satellites",
    "set_client_identifier",
    "user_agent",
    "CANDIDATE_COLUMNS",
    "in_orbit_candidates",
    "CacheValidationError",
    "SearchSnapshot",
    "TextOrbitCache",
    "read_legacy_tle_records",
    "read_orbit_file",
    "CHECKSUM_STATUS_FIELD",
    "CHECKSUM_UNVERIFIED_MISSING",
    "CHECKSUM_VERIFIED",
    "KIND_OMM",
    "KIND_TLE",
    "KIND_FIELD",
    "RecordKindError",
    "record_elements",
    "record_epoch_jd",
    "record_kind",
    "validate_record",
    "validated_record",
    "MAX_WORKERS",
    "NearestBatchResult",
    "fetch_nearest_batch",
    "store_or_warn",
)

#: Where each export lives, so a re-export cannot start pointing elsewhere.
EXPORT_HOMES = {
    "client": client,
    "records": records,
    "cache": cache_module,
    "service": service,
    "catalogue": catalogue,
    "tle_parse": tle_parse,
    "_time": _time,
    "_version": _version,
}

#: Which module defines each export, as of ``06dcbf5``. The signatures above
#: are checked on the defining modules and the names on the package; without
#: this the two could drift, and a package-level name rebound to something else
#: would satisfy both.
DEFINED_IN_AT_06DCBF5 = {
    "datetime_to_jd": "_time",
    "jd_to_datetime": "_time",
    "__version__": "_version",
    "BASE_URL": "client",
    "HANDOVER_JD": "client",
    "OMM_COLUMNS": "client",
    "SEARCH_COLUMNS": "client",
    "TLE_COLUMNS": "client",
    "fetch_nearest_omm": "client",
    "nearest_endpoints_for": "service",
    "SatCheckerError": "client",
    "SatCheckerRateLimitError": "client",
    "SatCheckerResponseError": "client",
    "SatCheckerTransportError": "client",
    "fetch_nearest_tle": "client",
    "search_satellites": "client",
    "set_client_identifier": "client",
    "user_agent": "client",
    "CANDIDATE_COLUMNS": "catalogue",
    "in_orbit_candidates": "catalogue",
    "CacheValidationError": "cache",
    "SearchSnapshot": "cache",
    "TextOrbitCache": "cache",
    "read_legacy_tle_records": "cache",
    "read_orbit_file": "cache",
    "CHECKSUM_STATUS_FIELD": "records",
    "CHECKSUM_UNVERIFIED_MISSING": "records",
    "CHECKSUM_VERIFIED": "records",
    "KIND_OMM": "records",
    "KIND_TLE": "records",
    "KIND_FIELD": "records",
    "RecordKindError": "records",
    "record_elements": "records",
    "record_epoch_jd": "records",
    "record_kind": "records",
    "validate_record": "records",
    "validated_record": "records",
    "MAX_WORKERS": "service",
    "NearestBatchResult": "service",
    "fetch_nearest_batch": "service",
    "store_or_warn": "service",
}

#: ``module.qualname`` -> the parameter list as of ``06dcbf5``.
SIGNATURES_AT_06DCBF5 = {
    "client.fetch_nearest_tle": (
        "norad_id:POSITIONAL_OR_KEYWORD",
        "epoch_jd:POSITIONAL_OR_KEYWORD",
        "strict_response:KEYWORD_ONLY=False",
    ),
    "client.fetch_nearest_omm": (
        "norad_id:POSITIONAL_OR_KEYWORD",
        "epoch_jd:POSITIONAL_OR_KEYWORD",
        "strict_response:KEYWORD_ONLY=False",
    ),
    "client.search_satellites": ("name:POSITIONAL_OR_KEYWORD",),
    "client.set_client_identifier": ("identifier:POSITIONAL_OR_KEYWORD",),
    "client.user_agent": (),
    "client.SatCheckerRateLimitError.__init__": (
        "self:POSITIONAL_OR_KEYWORD",
        "message:POSITIONAL_OR_KEYWORD",
        "retry_after:POSITIONAL_OR_KEYWORD=None",
    ),
    "client.SatCheckerResponseError.__init__": (
        "self:POSITIONAL_OR_KEYWORD",
        "message:POSITIONAL_OR_KEYWORD",
        "status:POSITIONAL_OR_KEYWORD=None",
    ),
    "records.record_kind": ("record:POSITIONAL_OR_KEYWORD",),
    "records.record_epoch_jd": ("record:POSITIONAL_OR_KEYWORD",),
    "records.record_elements": ("record:POSITIONAL_OR_KEYWORD",),
    "records.parse_omm_epoch_jd": ("value:POSITIONAL_OR_KEYWORD",),
    "records.norad_id_of": (
        "record:POSITIONAL_OR_KEYWORD",
        "context:POSITIONAL_OR_KEYWORD='record'",
    ),
    "records.validate_record": (
        "record:POSITIONAL_OR_KEYWORD",
        "allow_missing_checksum:KEYWORD_ONLY=False",
    ),
    "records.validated_record": (
        "record:POSITIONAL_OR_KEYWORD",
        "allow_missing_checksum:KEYWORD_ONLY=False",
    ),
    "cache.read_orbit_file": ("path:POSITIONAL_OR_KEYWORD",),
    "cache.read_legacy_tle_records": ("directory:POSITIONAL_OR_KEYWORD",),
    "cache.TextOrbitCache.__init__": (
        "self:POSITIONAL_OR_KEYWORD",
        "cache_dir:POSITIONAL_OR_KEYWORD",
    ),
    "cache.TextOrbitCache.path": (
        "self:POSITIONAL_OR_KEYWORD",
        "norad_id:POSITIONAL_OR_KEYWORD",
    ),
    "cache.TextOrbitCache.get": (
        "self:POSITIONAL_OR_KEYWORD",
        "norad_id:POSITIONAL_OR_KEYWORD",
        "log:POSITIONAL_OR_KEYWORD=builtins.print",
    ),
    "cache.TextOrbitCache.store": (
        "self:POSITIONAL_OR_KEYWORD",
        "norad_id:POSITIONAL_OR_KEYWORD",
        "records:POSITIONAL_OR_KEYWORD",
    ),
    "cache.TextOrbitCache.search_path": (
        "self:POSITIONAL_OR_KEYWORD",
        "name:POSITIONAL_OR_KEYWORD",
    ),
    "cache.TextOrbitCache.get_search": (
        "self:POSITIONAL_OR_KEYWORD",
        "name:POSITIONAL_OR_KEYWORD",
        "log:POSITIONAL_OR_KEYWORD=builtins.print",
    ),
    "cache.TextOrbitCache.store_search": (
        "self:POSITIONAL_OR_KEYWORD",
        "name:POSITIONAL_OR_KEYWORD",
        "found:POSITIONAL_OR_KEYWORD",
        "fetched_at:POSITIONAL_OR_KEYWORD=None",
    ),
    "service.nearest_endpoints_for": ("epoch_jd:POSITIONAL_OR_KEYWORD",),
    "service.fetch_nearest_batch": (
        "norad_ids:POSITIONAL_OR_KEYWORD",
        "epoch_jd:POSITIONAL_OR_KEYWORD",
        "fetch_nearest:KEYWORD_ONLY=satchecker_client.client.fetch_nearest_tle",
        "endpoint:KEYWORD_ONLY='nearest-TLE'",
        "max_workers:KEYWORD_ONLY=5",
        "log:KEYWORD_ONLY=builtins.print",
        "allow_missing_checksum:KEYWORD_ONLY=False",
    ),
    "service.validated_records": (
        "records:POSITIONAL_OR_KEYWORD",
        "context:POSITIONAL_OR_KEYWORD",
        "log:POSITIONAL_OR_KEYWORD=builtins.print",
        "allow_missing_checksum:KEYWORD_ONLY=False",
    ),
    "service.store_or_warn": (
        "action:POSITIONAL_OR_KEYWORD",
        "target:POSITIONAL_OR_KEYWORD",
        "what:POSITIONAL_OR_KEYWORD",
        "log:POSITIONAL_OR_KEYWORD=builtins.print",
    ),
    "tle_parse.decode_norad_id": ("field:POSITIONAL_OR_KEYWORD",),
    "tle_parse.tle_checksum": ("line:POSITIONAL_OR_KEYWORD",),
    "tle_parse.tle_epoch_jd": ("line1:POSITIONAL_OR_KEYWORD",),
    "tle_parse.tle_line_defects": ("line:POSITIONAL_OR_KEYWORD",),
    "tle_parse.parse_tle_elements": (
        "line1:POSITIONAL_OR_KEYWORD",
        "line2:POSITIONAL_OR_KEYWORD",
    ),
    "tle_parse.semimajor_axis_km": ("mean_motion_rev_day:POSITIONAL_OR_KEYWORD",),
    "tle_parse.validate_elements": (
        "elements:POSITIONAL_OR_KEYWORD",
        "context:POSITIONAL_OR_KEYWORD='orbital element set'",
    ),
    "tle_parse.validate_tle_line": (
        "line:POSITIONAL_OR_KEYWORD",
        "number:POSITIONAL_OR_KEYWORD",
        "allow_missing_checksum:KEYWORD_ONLY=False",
    ),
    "tle_parse.validate_tle_pair": (
        "line1:POSITIONAL_OR_KEYWORD",
        "line2:POSITIONAL_OR_KEYWORD",
        "allow_missing_checksum:KEYWORD_ONLY=False",
    ),
    "catalogue.in_orbit_candidates": (
        "found:POSITIONAL_OR_KEYWORD",
        "epoch_jd:POSITIONAL_OR_KEYWORD",
    ),
    "_time.datetime_to_jd": ("value:POSITIONAL_OR_KEYWORD",),
    "_time.jd_to_datetime": ("value:POSITIONAL_OR_KEYWORD",),
    "_time.is_iso_date": ("value:POSITIONAL_OR_KEYWORD",),
}


def _shape(function) -> tuple:
    """A signature as name, kind and default, with callables named not addressed."""
    shape = []
    for parameter in inspect.signature(function).parameters.values():
        default = parameter.default
        if default is inspect.Parameter.empty:
            token = ""
        elif inspect.isroutine(default) or inspect.isclass(default):
            token = f"={default.__module__}.{default.__qualname__}"
        else:
            token = f"={default!r}"
        shape.append(f"{parameter.name}:{parameter.kind.name}{token}")
    return tuple(shape)


def _resolve(dotted):
    module_name, _, attribute = dotted.partition(".")
    target = EXPORT_HOMES[module_name]
    for part in attribute.split("."):
        target = getattr(target, part)
    return target


# ---------------------------------------------------------------------------
# The surface itself
# ---------------------------------------------------------------------------

def test_existing_public_exports_and_signatures_are_preserved():
    missing = [name for name in EXPORTS_AT_06DCBF5 if name not in sc.__all__]
    assert missing == []
    for name in EXPORTS_AT_06DCBF5:
        assert hasattr(sc, name), name

    for dotted, expected in SIGNATURES_AT_06DCBF5.items():
        assert _shape(_resolve(dotted)) == expected, dotted

    # The batch's endpoint default is the client function itself, resolved at
    # import; a caller passing nothing gets the nearest-TLE endpoint.
    assert (
        inspect.signature(fetch_nearest_batch).parameters["fetch_nearest"].default
        is client.fetch_nearest_tle
    )


def test_existing_exports_are_the_objects_their_modules_define():
    """Every old package name is its defining module's object, by identity."""
    assert set(DEFINED_IN_AT_06DCBF5) == set(EXPORTS_AT_06DCBF5)
    for name, module_name in DEFINED_IN_AT_06DCBF5.items():
        assert getattr(sc, name) is getattr(EXPORT_HOMES[module_name], name), name


def test_existing_public_constants_are_preserved():
    assert sc.BASE_URL == "https://satchecker.cps.iau.org/tools"
    assert _time.jd_to_datetime(sc.HANDOVER_JD).date().isoformat() == "2026-07-12"
    assert sc.MAX_WORKERS == 5
    assert service.RESPONSE_WALL_THRESHOLD == 10
    assert sc.TLE_COLUMNS == [
        "NORAD_CAT_ID",
        "OBJECT_NAME",
        "EPOCH",
        "TLE_LINE1",
        "TLE_LINE2",
        "DATA_SOURCE",
        "DATE_COLLECTED",
    ]
    assert sc.SEARCH_COLUMNS == [
        "NORAD_CAT_ID",
        "OBJECT_NAME",
        "OBJECT_ID",
        "OBJECT_TYPE",
        "RCS_SIZE",
        "LAUNCH_DATE",
        "DECAY_DATE",
    ]
    assert sc.CHECKSUM_STATUS_FIELD == "TLE_CHECKSUM_STATUS"
    assert sc.CHECKSUM_VERIFIED == "verified"
    assert sc.CHECKSUM_UNVERIFIED_MISSING == "unverified_missing_checksum"
    assert (sc.KIND_TLE, sc.KIND_OMM, sc.KIND_FIELD) == ("tle", "omm", "RECORD_KIND")


def test_cache_schema_versions_are_unchanged(tmp_path):
    """The files are shared with every other application reading this cache."""
    assert (SCHEMA_VERSION, SEARCH_SCHEMA_VERSION) == (2, 1)
    cache = TextOrbitCache(tmp_path / "cache")
    line1, line2 = make_tle(A, OBS)
    frame = pd.DataFrame(
        [
            {
                "NORAD_CAT_ID": A,
                "RECORD_KIND": "tle",
                "TLE_LINE1": line1,
                "TLE_LINE2": line2,
            }
        ]
    )
    cache.store(A, frame)
    envelope = json.loads(cache.path(A).read_text())
    assert set(envelope) == {"schema_version", "norad_id", "records"}
    assert envelope["schema_version"] == 2
    assert cache.path(A).name == f"orbit-{A}.json"


# ---------------------------------------------------------------------------
# The new layer is additive, and nothing old goes through it
# ---------------------------------------------------------------------------

def test_importing_the_package_has_no_side_effects(monkeypatch):
    before = client.user_agent()
    monkeypatch.setattr(TextOrbitCache, "__init__", forbid("TextOrbitCache()"))
    monkeypatch.setattr(client, "_http_get", forbid("client._http_get"))

    importlib.reload(sc)

    assert client.user_agent() == before
    assert client._client_identifier is None


#: The optional modules, and the package attribute importing each one binds.
OPTIONAL_MODULES = ("satchecker_client.resolve", "satchecker_client.replay")


@pytest.fixture
def reimportable_optional_modules():
    """Let one test reimport the optional modules, and put every binding back.

    Importing a submodule binds it in two places — the ``sys.modules`` entry and
    an attribute on the package — so restoring only the first leaves the session
    with two module objects answering to one name: the package re-exporting the
    first one's classes while ``import`` hands out the second's, and a test that
    patches a name on one patching nothing the code under test uses. Both go
    back, and the finaliser says so rather than leaving the next test to find
    out.
    """
    saved = {name: sys.modules[name] for name in OPTIONAL_MODULES}
    yield OPTIONAL_MODULES
    for name, module in saved.items():
        attribute = name.rpartition(".")[2]
        sys.modules[name] = module
        setattr(sc, attribute, module)
        assert sys.modules[name] is module
        assert getattr(sc, attribute) is module


def test_importing_resolver_has_no_side_effects(
    monkeypatch, reimportable_optional_modules
):
    """Importing the optional layer must not reach a network, a cache or a name."""
    before = client.user_agent()
    monkeypatch.setattr(TextOrbitCache, "__init__", forbid("TextOrbitCache()"))
    monkeypatch.setattr(client, "_http_get", forbid("client._http_get"))
    monkeypatch.setattr(client, "set_client_identifier", forbid("set_client_identifier"))

    for name in reimportable_optional_modules:
        sys.modules.pop(name)
        importlib.import_module(name)

    assert client.user_agent() == before
    assert client._client_identifier is None


def test_the_optional_modules_have_one_identity_each():
    """Three ways of naming a submodule, one module object behind all of them.

    Follows the reimport above deliberately: this is what an incompletely
    restored import leaves behind, and it is invisible from inside the test
    that caused it.
    """
    for name in OPTIONAL_MODULES:
        attribute = name.rpartition(".")[2]
        assert getattr(sc, attribute) is sys.modules[name], name
        assert sys.modules[name] is importlib.import_module(name), name


def test_old_apis_do_not_delegate_to_new_layer(tmp_path, monkeypatch):
    """The old calls are the old implementations, not wrappers over the new one.

    A delegation would make every future change to the resolver a change to the
    contract an installed application already depends on.
    """
    resolve = resolve_module()
    replay = replay_module()
    for name in ("resolve_orbits", "read_extra_orbit_dir"):
        monkeypatch.setattr(resolve, name, forbid(f"resolve.{name}"))
    for name in ("save_orbits_for_reuse", "save_replay_orbits", "load_replay_orbits"):
        monkeypatch.setattr(replay, name, forbid(f"replay.{name}"))

    line1, line2 = make_tle(A, OBS)
    frame = pd.DataFrame(
        [
            {
                "NORAD_CAT_ID": A,
                "OBJECT_NAME": f"SAT-{A}",
                "RECORD_KIND": "tle",
                "TLE_LINE1": line1,
                "TLE_LINE2": line2,
                "DATA_SOURCE": "test",
            }
        ]
    )

    batch = fetch_nearest_batch(
        [A], OBS, fetch_nearest=lambda nid, epoch: frame.copy(), log=lambda _m: None
    )
    assert batch.records["NORAD_CAT_ID"].tolist() == [A]

    cache = TextOrbitCache(tmp_path / "cache")
    assert store_or_warn(
        lambda: cache.store(A, frame), cache.path(A), "orbit cache", log=lambda _m: None
    )
    assert len(cache.get(A, log=lambda _m: None)) == 1

    extra = tmp_path / "extra"
    extra.mkdir()
    write_legacy_tle_file(extra / "used_orbits.json", [(A, OBS)])
    assert read_legacy_tle_records(extra)["NORAD_CAT_ID"].tolist() == [A]
    assert sc.read_orbit_file(extra / "used_orbits.json")["NORAD_CAT_ID"].tolist() == [A]

    assert validate_record(frame.iloc[0]) == A
    assert sc.validated_record(frame.iloc[0])["NORAD_CAT_ID"] == A
    assert [label for label, _ in nearest_endpoints_for(OBS)] == [
        "nearest-TLE",
        "nearest-OMM",
    ]


# ---------------------------------------------------------------------------
# The call sequence tabascal makes today
# ---------------------------------------------------------------------------

def test_tabascal_current_call_pattern_remains_valid(tmp_path, monkeypatch):
    """tabascal's actual sequence, reproduced with client fixtures only.

    Its requirement on this package is unbounded, so this sequence is what an
    installed tabascal will run against whatever version it resolves. No
    consumer is imported: the point is that the client alone still behaves this
    way.
    """

    def serve(url, timeout=None):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        return make_nearest_json([(int(query["id"][0]), OBS)])

    monkeypatch.setattr(client, "_http_get", serve)
    logged = []

    endpoints = nearest_endpoints_for(OBS)
    assert [label for label, _ in endpoints] == ["nearest-TLE", "nearest-OMM"]
    label, fetch_nearest = endpoints[0]

    batch = fetch_nearest_batch(
        [A, B], OBS, fetch_nearest=fetch_nearest, endpoint=label, log=logged.append
    )
    assert batch.records["NORAD_CAT_ID"].tolist() == [A, B]
    assert batch.errors == {} and batch.outage is None
    assert list(batch.records.columns) == [*sc.TLE_COLUMNS, "RECORD_KIND"]

    cache = TextOrbitCache(tmp_path / "cache")
    for norad_id, rows in batch.records.groupby("NORAD_CAT_ID"):
        assert store_or_warn(
            lambda nid=int(norad_id), group=rows: cache.store(nid, group),
            cache.path(int(norad_id)),
            f"orbit cache for NORAD {int(norad_id)}",
            log=logged.append,
        )

    envelope = json.loads(cache.path(A).read_text())
    assert envelope["schema_version"] == 2
    assert envelope["norad_id"] == A
    (record,) = envelope["records"]
    assert {"NORAD_CAT_ID", "TLE_LINE1", "TLE_LINE2", "FETCHED_AT"} <= set(record)
    assert CHECKSUM_STATUS_FIELD not in record

    extra = tmp_path / "extra"
    extra.mkdir()
    write_legacy_tle_file(extra / "used_orbits_run.json", [(A, OBS)])
    supplied = read_legacy_tle_records(extra)
    row = supplied.iloc[0]
    assert validate_record(row) == int(row["NORAD_CAT_ID"])

    assert logged == []


def test_a_consumer_is_never_imported():
    """This package depends on neither application, at any import depth."""
    for name in list(sys.modules):
        assert not name.startswith(("tabsim", "tabascal")), name


@pytest.mark.parametrize("name", ["satchecker_client.resolve", "satchecker_client.replay"])
def test_the_new_modules_are_exported_at_package_level(name):
    module = importlib.import_module(name)
    for public in getattr(module, "__all__", []):
        assert getattr(sc, public) is getattr(module, public), public
        assert public in sc.__all__, public
