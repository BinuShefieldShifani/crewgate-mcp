"""HMAC-SHA256 signatures for partner traffic, in both directions.

Header format (the same one Duffel uses for webhooks):

    t=<unix seconds>,v1=<lowercase hex HMAC-SHA256>[,v1=<another signature>]

Inbound webhooks (Duffel): signed string is  b"<t>." + raw_body.
Outbound partner calls (ours): signed string is  b"<t>.<nonce>.<METHOD>.<path?query>." + raw_body,
so a captured request can't be replayed, re-pointed at another endpoint or given a new body.

Both sides check the timestamp is inside a tolerance window, and remember what they have seen
(nonces for requests, event ids for webhooks, since Duffel retries and can send duplicates).
Several secrets can be active at once, so keys can be rotated without downtime.
"""

import hashlib
import hmac
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass
from enum import StrEnum

import httpx
from pydantic import SecretStr

REQUEST_HEADER = "X-Crewgate-Signature"
DUFFEL_HEADER = "X-Duffel-Signature"


class SignatureFailure(StrEnum):
    MISSING = "missing_signature"
    MALFORMED = "malformed_signature"
    STALE = "stale_timestamp"
    MISMATCH = "signature_mismatch"
    REPLAY = "replayed"


class SignatureError(Exception):
    def __init__(self, reason: SignatureFailure) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class ParsedSignature:
    timestamp: int
    signatures: tuple[str, ...]
    nonce: str | None = None


def parse_header(value: str | None) -> ParsedSignature:
    if not value:
        raise SignatureError(SignatureFailure.MISSING)
    timestamp: int | None = None
    nonce: str | None = None
    sigs: list[str] = []
    for part in value.split(","):
        key, sep, val = part.strip().partition("=")
        if not sep or not val:
            raise SignatureError(SignatureFailure.MALFORMED)
        if key == "t":
            if not val.isdigit():
                raise SignatureError(SignatureFailure.MALFORMED)
            timestamp = int(val)
        elif key == "n":
            nonce = val
        elif key == "v1":
            sigs.append(val.lower())
        # unknown keys (future versions such as v2) are ignored on purpose
    if timestamp is None or not sigs:
        raise SignatureError(SignatureFailure.MALFORMED)
    return ParsedSignature(timestamp, tuple(sigs), nonce)


def hmac_hex(secret: SecretStr, message: bytes) -> str:
    return hmac.new(secret.get_secret_value().encode(), message, hashlib.sha256).hexdigest()


def _any_match(secrets_: Sequence[SecretStr], message: bytes, candidates: Sequence[str]) -> bool:
    matched = False
    for secret in secrets_:
        expected = hmac_hex(secret, message)
        for candidate in candidates:
            # compare_digest on every pair: no early exit that leaks timing
            matched |= hmac.compare_digest(expected, candidate)
    return matched


class SeenCache:
    """Bounded memory of nonces or event ids seen inside the tolerance window."""

    def __init__(self, ttl_seconds: float, max_entries: int = 100_000) -> None:
        self.ttl, self.max_entries = ttl_seconds, max_entries
        self._seen: OrderedDict[str, float] = OrderedDict()

    def add_if_new(self, key: str, now: float) -> bool:
        while self._seen:
            oldest, at = next(iter(self._seen.items()))
            if now - at < self.ttl and len(self._seen) < self.max_entries:
                break
            self._seen.pop(oldest)
        if key in self._seen:
            return False
        self._seen[key] = now
        return True


def request_message(timestamp: int, nonce: str, method: str, target: str, body: bytes) -> bytes:
    return f"{timestamp}.{nonce}.{method.upper()}.{target}.".encode() + body


def _target(url: httpx.URL) -> str:
    query = url.query.decode()
    return url.path + (f"?{query}" if query else "")


class RequestSigner:
    def __init__(self, secret: SecretStr, clock: Callable[[], float] = time.time) -> None:
        self.secret, self.clock = secret, clock

    def header(self, method: str, target: str, body: bytes) -> str:
        t = int(self.clock())
        nonce = secrets.token_urlsafe(16)
        sig = hmac_hex(self.secret, request_message(t, nonce, method, target, body))
        return f"t={t},n={nonce},v1={sig}"


class SigningAuth(httpx.Auth):
    """httpx auth flow that signs every request after its body is final."""

    requires_request_body = True

    def __init__(self, signer: RequestSigner) -> None:
        self.signer = signer

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers[REQUEST_HEADER] = self.signer.header(
            request.method, _target(request.url), request.content
        )
        yield request


class RequestVerifier:
    """Partner side: checks a signed request from the gateway."""

    def __init__(
        self,
        secrets_: Sequence[SecretStr],
        tolerance_seconds: float = 300.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not secrets_:
            raise ValueError("at least one secret is required")
        self.secrets, self.tolerance, self.clock = list(secrets_), tolerance_seconds, clock
        self.nonces = SeenCache(2 * tolerance_seconds)

    def verify(self, header: str | None, method: str, target: str, body: bytes) -> None:
        sig = parse_header(header)
        if sig.nonce is None:
            raise SignatureError(SignatureFailure.MALFORMED)
        now = self.clock()
        if abs(now - sig.timestamp) > self.tolerance:
            raise SignatureError(SignatureFailure.STALE)
        message = request_message(sig.timestamp, sig.nonce, method, target, body)
        if not _any_match(self.secrets, message, sig.signatures):
            raise SignatureError(SignatureFailure.MISMATCH)
        if not self.nonces.add_if_new(sig.nonce, now):  # only after the signature checks out
            raise SignatureError(SignatureFailure.REPLAY)


class WebhookVerifier:
    """Gateway side: checks a Duffel-style webhook (t=..,v1=.. over b"<t>." + raw body)."""

    def __init__(
        self,
        secrets_: Sequence[SecretStr],
        tolerance_seconds: float = 300.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not secrets_:
            raise ValueError("at least one secret is required")
        self.secrets, self.tolerance, self.clock = list(secrets_), tolerance_seconds, clock
        self.events = SeenCache(24 * 3600)

    def verify(self, header: str | None, raw_body: bytes) -> ParsedSignature:
        sig = parse_header(header)
        if abs(self.clock() - sig.timestamp) > self.tolerance:
            raise SignatureError(SignatureFailure.STALE)
        if not _any_match(self.secrets, f"{sig.timestamp}.".encode() + raw_body, sig.signatures):
            raise SignatureError(SignatureFailure.MISMATCH)
        return sig

    def first_delivery(self, event_id: str) -> bool:
        """False for a retried duplicate. Call after verify(), with the event's id or idempotency key."""
        return self.events.add_if_new(event_id, self.clock())


def sign_webhook(secret: SecretStr, raw_body: bytes, timestamp: int) -> str:
    """What the sender does. Used by tests and the local webhook simulator."""
    return f"t={timestamp},v1={hmac_hex(secret, f'{timestamp}.'.encode() + raw_body)}"
