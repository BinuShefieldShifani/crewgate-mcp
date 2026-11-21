"""Headless PKCE sign-in for CI and demos. DEV ONLY.

A real MCP client opens the user's browser. CI has no browser, so this helper fills in
Keycloak's login form itself. It still runs the full authorization-code + PKCE flow, so the
tokens it gets are the same kind a real client gets. Never use it with real user accounts.
"""

import html
import re
import secrets
from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

from .pkce import AuthServerMetadata, PkcePair, build_authorization_url, exchange_code

_FORM_ACTION = re.compile(r'<form[^>]*\baction="([^"]+)"', re.IGNORECASE)


class LoginError(Exception):
    pass


async def headless_login(
    http: httpx.AsyncClient,
    server: AuthServerMetadata,
    *,
    client_id: str,
    redirect_uri: str,
    username: str,
    password: str,
    scopes: Iterable[str],
    resource: str | None = None,
) -> dict[str, Any]:
    pkce = PkcePair.generate()
    state = secrets.token_urlsafe(16)
    url = build_authorization_url(
        server.authorization_endpoint,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=scopes,
        state=state,
        pkce=pkce,
        resource=resource,
    )
    page = await http.get(url, follow_redirects=False)
    match = _FORM_ACTION.search(page.text)
    if page.status_code != 200 or not match:
        raise LoginError(f"no login form (status {page.status_code})")

    # Python's cookie jar drops cookies set for "localhost", so send the session cookies by hand.
    cookie = "; ".join(f"{k}={v}" for k, v in page.cookies.items())
    answer = await http.post(
        html.unescape(match.group(1)),
        data={"username": username, "password": password},
        headers={"Cookie": cookie},
        follow_redirects=False,
    )
    location = answer.headers.get("location", "")
    query = dict(parse_qsl(urlsplit(location).query))
    if answer.status_code not in (302, 303) or not location.startswith(redirect_uri):
        raise LoginError(f"login rejected (status {answer.status_code})")
    if "error" in query:
        raise LoginError(query["error"])
    if query.get("state") != state:  # CSRF defence: the redirect must answer our own request
        raise LoginError("state mismatch")

    return await exchange_code(
        http,
        server.token_endpoint,
        client_id=client_id,
        code=query["code"],
        redirect_uri=redirect_uri,
        pkce=pkce,
        resource=resource,
    )
