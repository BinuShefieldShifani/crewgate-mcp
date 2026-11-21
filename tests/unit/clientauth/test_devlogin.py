"""The headless login must still do real PKCE and must catch state and error redirects."""

import base64
import hashlib
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from crewgate.clientauth import AuthServerMetadata
from crewgate.clientauth.devlogin import LoginError, headless_login

REDIRECT = "http://127.0.0.1:8765/callback"
SERVER = AuthServerMetadata(
    issuer="https://idp.test/realms/crewgate",
    authorization_endpoint="https://idp.test/auth",
    token_endpoint="https://idp.test/token",
    jwks_uri=None,
    code_challenge_methods_supported=("S256",),
)


class FakeLoginPage:
    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.challenge = ""
        self.state = ""

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        if url.path == "/auth":
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            self.challenge, self.state = q["code_challenge"], q["state"]
            if self.mode == "no-form":
                return httpx.Response(200, text="<html>maintenance</html>")
            page = (
                '<form id="kc-form-login" action="https://idp.test/login?session=1&amp;tab=2" method="post">'
            )
            return httpx.Response(200, text=page, headers={"set-cookie": "AUTH_SESSION_ID=abc; Path=/"})
        if url.path == "/login":
            assert request.headers["cookie"] == "AUTH_SESSION_ID=abc"
            assert "tab=2" in str(request.url)  # &amp; was unescaped
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if self.mode == "bad-password" or form["password"] != "dev-only-anna":
                return httpx.Response(200, text="Invalid username or password")
            params = {"code": "the-code", "state": self.state}
            if self.mode == "error":
                params = {"error": "access_denied", "state": self.state}
            if self.mode == "wrong-state":
                params = {"code": "the-code", "state": "someone-elses"}
            return httpx.Response(302, headers={"location": f"{REDIRECT}?{urlencode(params)}"})
        if url.path == "/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            digest = hashlib.sha256(form["code_verifier"].encode()).digest()
            if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != self.challenge:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "at-for-anna", "expires_in": 300})
        return httpx.Response(404)


async def login(page: FakeLoginPage, password: str = "dev-only-anna") -> dict[str, object]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(page.handler)) as http:
        return await headless_login(
            http,
            SERVER,
            client_id="crewgate-mcp-client",
            redirect_uri=REDIRECT,
            username="anna",
            password=password,
            scopes=["openid", "roster.read"],
            resource="http://localhost:8000/mcp",
        )


async def test_full_pkce_round_trip() -> None:
    assert (await login(FakeLoginPage()))["access_token"] == "at-for-anna"


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("no-form", "no login form"),
        ("bad-password", "login rejected"),
        ("error", "access_denied"),
        ("wrong-state", "state mismatch"),
    ],
)
async def test_failures(mode: str, message: str) -> None:
    with pytest.raises(LoginError, match=message):
        await login(FakeLoginPage(mode))
