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
