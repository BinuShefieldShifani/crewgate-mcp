"""Signatures: Duffel compatibility, tampering, replay, staleness, rotation, httpx integration."""

import hashlib
import hmac

import httpx
import pytest
from pydantic import SecretStr

from crewgate.core.signing import (
    REQUEST_HEADER,
    RequestSigner,
    RequestVerifier,
    SeenCache,
    SignatureError,
    SignatureFailure,
    SigningAuth,
    WebhookVerifier,
    parse_header,
    request_message,
    sign_webhook,
)

SECRET = SecretStr("dev-only-hmac-secret")
OLD = SecretStr("dev-only-hmac-old")
NOW = 1_800_000_000


class Clock:
    def __init__(self, t: float = NOW) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def fails(reason: SignatureFailure):  # type: ignore[no-untyped-def]
    return pytest.raises(SignatureError, match=reason.value)


# ---- header parsing -----------------------------------------------------------------------


def test_parse_duffel_example() -> None:
    header = "t=1616202842,v1=8aebaa7ecaf36950721e4321b6a56d7493d13e73814de672ac5ce4ddd7435054"
    p = parse_header(header)
    assert p.timestamp == 1616202842
    assert p.signatures == ("8aebaa7ecaf36950721e4321b6a56d7493d13e73814de672ac5ce4ddd7435054",)


def test_parse_multiple_signatures_and_ignores_unknown_versions() -> None:
    p = parse_header("t=1, v1=AA, v2=zz, v1=bb, n=abc")
    assert p.signatures == ("aa", "bb")
    assert p.nonce == "abc"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (None, SignatureFailure.MISSING),
        ("", SignatureFailure.MISSING),
        ("v1=abc", SignatureFailure.MALFORMED),
        ("t=1", SignatureFailure.MALFORMED),
        ("t=abc,v1=ff", SignatureFailure.MALFORMED),
        ("t=1,v1", SignatureFailure.MALFORMED),
        ("t=1,v1=", SignatureFailure.MALFORMED),
    ],
)
def test_parse_rejects(value: str | None, reason: SignatureFailure) -> None:
    with fails(reason):
        parse_header(value)


# ---- webhooks (Duffel scheme) -------------------------------------------------------------


def test_webhook_matches_duffels_reference_algorithm() -> None:
    body = b'{"id":"wev_1","type":"order.created"}'
    header = sign_webhook(SECRET, body, NOW)
    # Duffel's own sample: hmac(secret, t + "." + payload, sha256), lowercase hex
    expected = hmac.new(b"dev-only-hmac-secret", f"{NOW}.".encode() + body, hashlib.sha256).hexdigest()
    assert header == f"t={NOW},v1={expected}"
    assert WebhookVerifier([SECRET], clock=Clock()).verify(header, body).timestamp == NOW


def test_webhook_tampered_body() -> None:
    header = sign_webhook(SECRET, b'{"amount":"100"}', NOW)
    with fails(SignatureFailure.MISMATCH):
        WebhookVerifier([SECRET], clock=Clock()).verify(header, b'{"amount":"1"}')


def test_webhook_wrong_secret() -> None:
    header = sign_webhook(SecretStr("dev-only-attacker"), b"{}", NOW)
    with fails(SignatureFailure.MISMATCH):
        WebhookVerifier([SECRET], clock=Clock()).verify(header, b"{}")


def test_webhook_too_old_or_from_the_future() -> None:
    v = WebhookVerifier([SECRET], tolerance_seconds=300, clock=Clock())
    for t in (NOW - 301, NOW + 301):
        with fails(SignatureFailure.STALE):
            v.verify(sign_webhook(SECRET, b"{}", t), b"{}")
    v.verify(sign_webhook(SECRET, b"{}", NOW - 299), b"{}")


def test_webhook_secret_rotation() -> None:
    v = WebhookVerifier([SECRET, OLD], clock=Clock())
    v.verify(sign_webhook(OLD, b"{}", NOW), b"{}")  # still signed with the old key
    both = f"{sign_webhook(SECRET, b'{}', NOW)},v1={sign_webhook(OLD, b'{}', NOW).split('v1=')[1]}"
    v.verify(both, b"{}")


def test_webhook_duplicates_are_detected() -> None:
    v = WebhookVerifier([SECRET], clock=Clock())
    assert v.first_delivery("wev_1")
    assert not v.first_delivery("wev_1")
    assert v.first_delivery("wev_2")


def test_verifiers_need_a_secret() -> None:
    with pytest.raises(ValueError, match="secret"):
        WebhookVerifier([])
    with pytest.raises(ValueError, match="secret"):
        RequestVerifier([])


# ---- outbound requests ---------------------------------------------------------------------


def signed(
    method: str = "POST", target: str = "/swaps", body: bytes = b'{"a":1}', clock: Clock | None = None
) -> str:
    return RequestSigner(SECRET, clock=clock or Clock()).header(method, target, body)


def test_signed_request_verifies() -> None:
    RequestVerifier([SECRET], clock=Clock()).verify(signed(), "POST", "/swaps", b'{"a":1}')


@pytest.mark.parametrize(
    ("method", "target", "body"),
    [("GET", "/swaps", b'{"a":1}'), ("POST", "/crew", b'{"a":1}'), ("POST", "/swaps", b'{"a":2}')],
)
def test_request_cannot_be_repointed_or_rewritten(method: str, target: str, body: bytes) -> None:
    with fails(SignatureFailure.MISMATCH):
        RequestVerifier([SECRET], clock=Clock()).verify(signed(), method, target, body)


def test_request_replay_is_refused() -> None:
    v = RequestVerifier([SECRET], clock=Clock())
    header = signed()
    v.verify(header, "POST", "/swaps", b'{"a":1}')
    with fails(SignatureFailure.REPLAY):
        v.verify(header, "POST", "/swaps", b'{"a":1}')


def test_forged_request_does_not_burn_the_nonce() -> None:
    v = RequestVerifier([SECRET], clock=Clock())
    header = signed()
    forged = header.rsplit("v1=", 1)[0] + "v1=" + "0" * 64
    with fails(SignatureFailure.MISMATCH):
        v.verify(forged, "POST", "/swaps", b'{"a":1}')
    v.verify(header, "POST", "/swaps", b'{"a":1}')  # the genuine one still works


def test_request_without_nonce_is_malformed() -> None:
    sig = hmac.new(b"dev-only-hmac-secret", b"x", hashlib.sha256).hexdigest()
    with fails(SignatureFailure.MALFORMED):
        RequestVerifier([SECRET], clock=Clock()).verify(f"t={NOW},v1={sig}", "GET", "/", b"")


def test_stale_request() -> None:
    with fails(SignatureFailure.STALE):
        RequestVerifier([SECRET], clock=Clock(NOW + 600)).verify(signed(), "POST", "/swaps", b'{"a":1}')


def test_message_layout() -> None:
    assert request_message(1, "n", "post", "/x?a=1", b"{}") == b"1.n.POST./x?a=1.{}"


def test_seen_cache_expires_and_is_bounded() -> None:
    c = SeenCache(ttl_seconds=10, max_entries=2)
    assert c.add_if_new("a", 0)
    assert not c.add_if_new("a", 5)
    assert c.add_if_new("a", 11)  # expired, so it is new again
    c.add_if_new("b", 11)
    c.add_if_new("c", 11)  # evicts the oldest to stay within 2
    assert len(c._seen) == 2


async def test_httpx_auth_signs_the_final_request() -> None:
    seen: dict[str, str] = {}
    verifier = RequestVerifier([SECRET])

    def handler(request: httpx.Request) -> httpx.Response:
        target = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        verifier.verify(request.headers.get(REQUEST_HEADER), request.method, target, request.content)
        seen["ok"] = "yes"
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://p", auth=SigningAuth(RequestSigner(SECRET))
    ) as http:
        await http.post("/swaps", json={"requester_id": "C0001"}, params={"dry_run": "1"})
        await http.get("/crew")
    assert seen["ok"] == "yes"
