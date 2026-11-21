"""The mock partner must be as strict as a real API, or the client tests prove nothing."""


async def _token(http, client_id="crewgate-agent", secret="dev-only-secret", scope=""):
    return await http.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": secret,
            "scope": scope,
        },
    )


async def test_health(raw_http):
    assert (await raw_http().get("/health")).json() == {"status": "ok"}


async def test_unsupported_grant_type(raw_http):
    r = await raw_http().post(
        "/oauth/token",
        data={"grant_type": "password", "client_id": "crewgate-agent", "client_secret": "dev-only-secret"},
    )
    assert r.status_code == 400


async def test_bad_client_secret_rejected(raw_http):
    assert (await _token(raw_http(), secret="wrong")).status_code == 401


async def test_cannot_request_scope_beyond_grant(raw_http):
    r = await _token(raw_http(), client_id="readonly-agent", secret="dev-only-readonly", scope="swap.write")
    assert r.status_code == 400


async def test_missing_and_garbage_tokens_are_401(raw_http):
    http = raw_http()
    assert (await http.get("/crew")).status_code == 401
    assert (await http.get("/crew", headers={"Authorization": "Bearer nope"})).status_code == 401


async def test_read_only_token_cannot_write(raw_http):
    http = raw_http()
    tok = (await _token(http, client_id="readonly-agent", secret="dev-only-readonly")).json()["access_token"]
    r = await http.post(
        "/swaps",
        json={"requester_id": "C0001", "partner_id": "C0002", "date": "2026-12-03"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 403


async def test_expired_token_is_401(raw_http, clock):
    http = raw_http({"token_ttl_seconds": 60})
    tok = (await _token(http)).json()["access_token"]
    clock.t += 61
    r = await http.get("/crew", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401 and r.json()["detail"] == "token_expired"


async def test_rate_limit_returns_429_with_retry_after(raw_http):
    http = raw_http({"rate_limit_per_minute": 3})
    h = {"Authorization": f"Bearer {(await _token(http)).json()['access_token']}"}
    codes = [(await http.get("/crew", headers=h)).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    assert int((await http.get("/crew", headers=h)).headers["Retry-After"]) >= 1


async def test_page_size_is_capped_and_bad_cursor_rejected(raw_http):
    http = raw_http({"page_size_max": 50})
    h = {"Authorization": f"Bearer {(await _token(http)).json()['access_token']}"}
    body = (await http.get("/crew", params={"limit": 500}, headers=h)).json()
    assert len(body["items"]) == 50 and body["next_cursor"]
    assert (await http.get("/crew", params={"cursor": "%%%"}, headers=h)).status_code == 400


async def test_unknown_crew_is_404(raw_http):
    http = raw_http()
    h = {"Authorization": f"Bearer {(await _token(http)).json()['access_token']}"}
    assert (await http.get("/crew/C9999/roster", headers=h)).status_code == 404
    r = await http.post(
        "/swaps", json={"requester_id": "C0001", "partner_id": "C9999", "date": "x"}, headers=h
    )
    assert r.status_code == 404


# ---- HMAC request signatures --------------------------------------------------------------


def _signed_app(clock):  # type: ignore[no-untyped-def]
    from pydantic import SecretStr

    from mock_roster_api.app import create_app
    from mock_roster_api.settings import ApiSettings

    return create_app(ApiSettings(hmac_secrets=[SecretStr("dev-only-hmac-secret")]), clock=clock)


async def test_signed_client_calls_are_accepted(clock):  # type: ignore[no-untyped-def]
    import httpx

    from crewgate.plugins.crew.client import RosterClient
    from crewgate.plugins.crew.config import CrewSettings

    transport = httpx.ASGITransport(app=_signed_app(clock))
    async with RosterClient(CrewSettings(api_base_url="http://mock"), transport=transport, clock=clock) as c:
        assert len([x async for x in c.iter_crew(page_size=50)]) == 120  # GET with a query string
        assert (await c.request_swap("C0001", "C0002", "2026-12-03"))[
            "status"
        ] == "pending_approval"  # POST body


async def test_unsigned_tampered_and_replayed_calls_are_refused(clock):  # type: ignore[no-untyped-def]
    import httpx
    from pydantic import SecretStr

    from crewgate.core.signing import REQUEST_HEADER, RequestSigner

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_signed_app(clock)), base_url="http://mock"
    ) as http:
        assert (await http.get("/health")).status_code == 200  # health checks stay open
        unsigned = await http.post("/oauth/token", data={"grant_type": "client_credentials"})
        assert unsigned.status_code == 401 and unsigned.json()["detail"] == "missing_signature"

        body = b"grant_type=client_credentials&client_id=crewgate-agent&client_secret=dev-only-secret"
        header = RequestSigner(SecretStr("dev-only-hmac-secret"), clock=clock).header(
            "POST", "/oauth/token", body
        )
        form = {"content-type": "application/x-www-form-urlencoded", REQUEST_HEADER: header}
        assert (await http.post("/oauth/token", content=body, headers=form)).status_code == 200

        replay = await http.post("/oauth/token", content=body, headers=form)
        assert replay.json()["detail"] == "replayed"

        fresh = RequestSigner(SecretStr("dev-only-hmac-secret"), clock=clock).header(
            "POST", "/oauth/token", body
        )
        tampered = await http.post(
            "/oauth/token",
            content=body.replace(b"crewgate-agent", b"readonly-agent"),
            headers={**form, REQUEST_HEADER: fresh},
        )
        assert tampered.json()["detail"] == "signature_mismatch"
