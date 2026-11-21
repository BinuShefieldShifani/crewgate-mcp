"""Every way a token can be wrong must end in invalid_token, never in a crash or a pass."""

import base64
import hashlib
import hmac
import json

import jwt
import pytest
from cryptography.hazmat.primitives import serialization

from crewgate.core.auth import AuthError, AuthErrorCode, JwksCache, TokenVerifier
from tests.helpers.idp import GATEWAY, ISSUER, JWKS_URL, PARTNER, FakeIdp, ec_key, rsa_key


async def _rejects(verifier: TokenVerifier, token: str, reason: str) -> None:
    with pytest.raises(AuthError) as err:
        await verifier.verify(token)
    assert err.value.code is AuthErrorCode.INVALID_TOKEN
    assert err.value.status == 401
    assert reason in err.value.description


async def test_valid_token_becomes_principal(idp: FakeIdp, verifier: TokenVerifier) -> None:
    token = idp.token(roles={PARTNER: ["roster-reader"]})
    p = await verifier.verify(token)
    assert p.subject == "user-anna"
    assert p.client_id == "crewgate-mcp-client"
    assert p.username == "anna"
    assert p.scopes == {"openid", "roster.read", "swap.write"}
    assert p.roles_for(PARTNER) == {"roster-reader"}
    assert p.token.get_secret_value() == token
    assert token not in repr(p)  # the raw token never shows up in logs


async def test_ec_keys_work_too() -> None:
    idp = FakeIdp(keys=[ec_key()])
    v = TokenVerifier(ISSUER, GATEWAY, JwksCache(JWKS_URL, idp.client()))
    assert (await v.verify(idp.token())).subject == "user-anna"


async def test_expired(idp: FakeIdp, verifier: TokenVerifier) -> None:
    await _rejects(verifier, idp.token(exp_in=-120), "expired")


async def test_leeway_tolerates_small_clock_skew(idp: FakeIdp, verifier: TokenVerifier) -> None:
    assert (await verifier.verify(idp.token(exp_in=-10))).subject == "user-anna"


async def test_partner_token_cannot_be_replayed_at_gateway(idp: FakeIdp, verifier: TokenVerifier) -> None:
    await _rejects(verifier, idp.token(aud=PARTNER), "not issued for this resource")


async def test_wrong_issuer(idp: FakeIdp, verifier: TokenVerifier) -> None:
    await _rejects(verifier, idp.token(iss="https://evil.test/realms/crewgate"), "untrusted issuer")


async def test_alg_none_rejected(idp: FakeIdp, verifier: TokenVerifier) -> None:
    def seg(d: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    token = f"{seg({'alg': 'none', 'kid': 'rsa-1'})}.{seg({'sub': 'x', 'aud': GATEWAY, 'iss': ISSUER})}."
    await _rejects(verifier, token, "unsupported signing algorithm")


async def test_hmac_with_public_key_as_secret_rejected(idp: FakeIdp, verifier: TokenVerifier) -> None:
    """Classic key-confusion attack: sign HS256 using the server's own public key as the secret."""
    pem = (
        idp.keys[0]
        .private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )

    def seg(d: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    # PyJWT refuses to build this token, so forge it by hand the way an attacker would.
    signing_input = (
        f"{seg({'alg': 'HS256', 'kid': 'rsa-1'})}.{seg({'sub': 'x', 'aud': GATEWAY, 'iss': ISSUER})}"
    )
    mac = hmac.new(pem, signing_input.encode(), hashlib.sha256).digest()
    forged = f"{signing_input}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"
    await _rejects(verifier, forged, "unsupported signing algorithm")


async def test_missing_kid(idp: FakeIdp, verifier: TokenVerifier) -> None:
    key = idp.keys[0]
    token = jwt.encode({"sub": "x"}, key.private, algorithm="RS256")
    await _rejects(verifier, token, "no key id")


async def test_unknown_kid(idp: FakeIdp, verifier: TokenVerifier) -> None:
    stranger = rsa_key(kid="not-published")
    await _rejects(verifier, idp.token(key=stranger), "unknown signing key")


async def test_signed_by_wrong_key_with_valid_kid(idp: FakeIdp, verifier: TokenVerifier) -> None:
    impostor = rsa_key(kid="rsa-1")  # same kid, different private key
    await _rejects(verifier, idp.token(key=impostor), "invalid token")


async def test_tampered_payload(idp: FakeIdp, verifier: TokenVerifier) -> None:
    header, payload, sig = idp.token().split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["scope"] = "openid roster.read swap.write admin"
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    await _rejects(verifier, f"{header}.{forged}.{sig}", "invalid token")


async def test_alg_must_match_declared_key_alg(idp: FakeIdp, verifier: TokenVerifier) -> None:
    await _rejects(verifier, idp.token(alg="PS256"), "does not match signing key")


async def test_key_without_declared_alg_accepts_any_allowed_alg() -> None:
    idp = FakeIdp(keys=[rsa_key(alg=None)])
    v = TokenVerifier(ISSUER, GATEWAY, JwksCache(JWKS_URL, idp.client()))
    assert (await v.verify(idp.token(alg="PS256"))).subject == "user-anna"


async def test_id_token_rejected(idp: FakeIdp, verifier: TokenVerifier) -> None:
    await _rejects(verifier, idp.token(typ="ID"), "not an access token")


async def test_unexpected_header_typ_rejected(idp: FakeIdp, verifier: TokenVerifier) -> None:
    await _rejects(verifier, idp.token(headers={"typ": "dpop+jwt"}), "not an access token")


async def test_rfc9068_header_typ_accepted(idp: FakeIdp, verifier: TokenVerifier) -> None:
    assert (await verifier.verify(idp.token(headers={"typ": "at+jwt"}))).subject == "user-anna"


@pytest.mark.parametrize("claim", ["sub", "exp", "iat"])
async def test_required_claims(idp: FakeIdp, verifier: TokenVerifier, claim: str) -> None:
    await _rejects(verifier, idp.token(drop=(claim,)), "invalid token")


async def test_garbage(verifier: TokenVerifier) -> None:
    await _rejects(verifier, "not-a-jwt", "malformed token")


def test_symmetric_algorithms_cannot_be_enabled(idp: FakeIdp) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        TokenVerifier(ISSUER, GATEWAY, JwksCache(JWKS_URL, idp.client()), algorithms=["RS256", "HS256"])


async def test_scope_check(idp: FakeIdp, verifier: TokenVerifier) -> None:
    p = await verifier.verify(idp.token(scope="openid roster.read"))
    p.require_scopes(["roster.read"])
    with pytest.raises(AuthError) as err:
        p.require_scopes(["roster.read", "swap.write"])
    assert err.value.code is AuthErrorCode.INSUFFICIENT_SCOPE
    assert err.value.status == 403
    assert err.value.scope == "roster.read swap.write"
