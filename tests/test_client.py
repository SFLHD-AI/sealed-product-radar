import json

import httpx
import pytest

from edge.tpx.client import (ReadOnlyViolation, TPXAuthError, TPXClient, TPXDomainError, TPXGraphQLError,
                             TPXTransportError)

Q = "query EdgeX($input: X!) { getOrderBook(input: $input) { error { message code } sequence } }"


def client_with(handler, **kw):
    return TPXClient("tp_test", "https://example.invalid/graphql", transport=httpx.MockTransport(handler),
                     rate_limit_rps=1000, sleep=lambda s: None, **kw)


def test_sends_x_api_key_header_only():
    seen = {}

    def h(req):
        seen.update(req.headers)
        return httpx.Response(200, json={"data": {"getOrderBook": {"sequence": "1"}}})

    c = client_with(h)
    assert c.execute(Q, {"input": {}})["sequence"] == "1"
    assert seen["x-api-key"] == "tp_test"
    assert "authorization" not in seen


def test_top_level_errors_checked_before_data_auth():
    def h(req):
        return httpx.Response(200, json={"errors": [{"message": "Unauthenticated", "extensions": {"code": "UNAUTHENTICATED"}}],
                                         "data": {"getOrderBook": {"sequence": "99"}}})

    with pytest.raises(TPXAuthError):
        client_with(h).execute(Q, {"input": {}})


def test_top_level_generic_error():
    def h(req):
        return httpx.Response(200, json={"errors": [{"message": "Cannot query field foo"}]})

    with pytest.raises(TPXGraphQLError, match="foo"):
        client_with(h).execute(Q, {"input": {}})


def test_per_payload_domain_error():
    def h(req):
        return httpx.Response(200, json={"data": {"getOrderBook": {"error": {"message": "not found", "code": "NOT_FOUND"}}}})

    with pytest.raises(TPXDomainError) as ei:
        client_with(h).execute(Q, {"input": {}})
    assert ei.value.code == "NOT_FOUND"


def test_retries_on_5xx_then_succeeds():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": {"getOrderBook": {"sequence": "7"}}})

    c = client_with(h, max_retries=4)
    assert c.execute(Q, {"input": {}})["sequence"] == "7"
    assert calls["n"] == 3


def test_gives_up_after_max_retries():
    def h(req):
        return httpx.Response(429, headers={"Retry-After": "1"})

    with pytest.raises(TPXTransportError):
        client_with(h, max_retries=2).execute(Q, {"input": {}})


def test_cache_hit_avoids_second_request():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        return httpx.Response(200, json={"data": {"getOrderBook": {"sequence": "1"}}})

    c = client_with(h)
    c.execute(Q, {"input": {"a": 1}})
    c.execute(Q, {"input": {"a": 1}})
    c.execute(Q, {"input": {"a": 2}})
    assert calls["n"] == 2


def test_mutations_are_refused_client_side():
    c = client_with(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ReadOnlyViolation):
        c.execute("mutation M { createOrderSubmission(input: {}) { id } }")
    with pytest.raises(ReadOnlyViolation):
        c.execute("query Q { createOrderSubmission { id } }")


def test_recorder_writes_fixture(tmp_path):
    from edge.tpx.client import FixtureRecorder

    c = client_with(lambda r: httpx.Response(200, json={"data": {"getOrderBook": {"sequence": "1"}}}),
                    recorder=FixtureRecorder(tmp_path))
    c.execute(Q, {"input": {"x": 1}})
    files = list(tmp_path.glob("EdgeX__*.json"))
    assert len(files) == 1
    doc = json.loads(files[0].read_text())
    assert doc["operation"] == "EdgeX" and doc["response"]["data"]["getOrderBook"]["sequence"] == "1"
