from __future__ import annotations

import copy

import pytest

from api_wrapper.config.loader import ConfigError, parse_config

BASE = {
    "project": "t",
    "sources": {"s": {"path": "/api/{claim_id}/x"}},
    "fields": [{"name": "a", "group": "g", "source": "s", "path": "$.A"}],
}


def mk(**over):
    d = copy.deepcopy(BASE)
    for k, v in over.items():
        d[k] = v
    return d


def errors(doc):
    with pytest.raises(ConfigError) as e:
        parse_config(doc)
    return "\n".join(e.value.errors)


def test_minimal_config_is_valid():
    assert parse_config(BASE).project == "t"


def test_unknown_key_is_rejected_not_ignored():
    assert "extra" in errors(mk(bogus=1)).lower() or "bogus" in errors(mk(bogus=1))


def test_unbound_placeholder_names_the_fix():
    msg = errors(mk(sources={"s": {"path": "/api/{claim_id}/{subscriber_key}"}}))
    assert "subscriber_key" in msg and "not bound" in msg


def test_bad_jsonpath_unknown_transform_missing_source():
    f = {"name": "a", "group": "g", "source": "nope", "path": "$.[[["}
    msg = errors(mk(fields=[f]))
    assert "unknown source" in msg and "invalid JSONPath" in msg
    f2 = {"name": "a", "group": "g", "source": "s", "path": "$.A", "transform": "magic"}
    assert "unknown transform" in errors(mk(fields=[f2]))


def test_multi_path_needs_multi_transform():
    f = {"name": "a", "group": "g", "source": "s", "paths": ["$.A", "$.B"]}
    assert "multi-value transform" in errors(mk(fields=[f]))
    f["transform"] = "upper"  # single-value transform with 2 paths
    assert "takes one path" in errors(mk(fields=[f]))


def test_dependency_cycle_detected():
    srcs = {
        "a": {"path": "/a/{x}", "params": {"x": {"from_source": "b", "path": "$.v"}}},
        "b": {"path": "/b/{y}", "params": {"y": {"from_source": "a", "path": "$.v"}}},
    }
    f = [{"name": "a", "group": "g", "source": "a", "path": "$.A"}]
    assert "cycle" in errors(mk(sources=srcs, fields=f))


def test_path_must_not_embed_host_or_query():
    assert "host" in errors(mk(sources={"s": {"path": "https://evil.example/x"}}))
    assert "query" in errors(mk(sources={"s": {"path": "/x?a=1"}}))


def test_param_cannot_come_from_foreach_source():
    srcs = {
        "lines": {"path": "/l/{claim_id}/{n}", "foreach": {"param": "n"}},
        "other": {"path": "/o/{k}", "params": {"k": {"from_source": "lines", "path": "$.v"}}},
    }
    f = [{"name": "a", "group": "g", "source": "other", "path": "$.A"}]
    assert "foreach" in errors(mk(sources=srcs, fields=f))


def test_row_key_name_is_reserved_in_array_groups():
    srcs = {"lines": {"path": "/l/{claim_id}/{line_no}", "foreach": {"param": "line_no"}}}
    f = [{"name": "line_no", "group": "g", "source": "lines", "path": "$.A"}]
    assert "reserved" in errors(mk(sources=srcs, fields=f))


def test_from_source_discovery_requires_source_and_path():
    srcs = {"lines": {"path": "/l/{claim_id}/{n}", "foreach": {"param": "n", "discovery": {"mode": "from_source"}}}}
    f = [{"name": "a", "group": "g", "source": "lines", "path": "$.A"}]
    assert "needs 'source' and 'path'" in errors(mk(sources=srcs, fields=f))


def test_all_errors_reported_at_once():
    f = [{"name": "A B", "group": "g", "source": "nope", "path": "$.A"}]
    msg = errors(mk(fields=f, sources={"s": {"path": "no-slash"}}))
    assert msg.count("\n") >= 2
