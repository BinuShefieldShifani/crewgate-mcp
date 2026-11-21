"""A write only happens after the same person signs in again, fresh, in the approval app."""

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from crewgate.core.audit import verify_chain

from .conftest import BASE, Env

SWAP = {"requester_id": "C0001", "partner_id": "C0002", "date": "2026-12-03", "reason": "Sick <b>leave</b>"}


async def propose(env: Env, user: str = "anna") -> dict[str, Any]:
    result = (await env.call("crew_propose_swap", SWAP, token=env.agent_token(user)))["result"]
    assert result["isError"] is False
    pending: dict[str, Any] = result["structuredContent"]
    return pending


async def decide(env: Env, approval_id: str, token: str | None, decision: str = "approve") -> Any:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return await env.http.post(
        f"/approvals/{approval_id}/decision", json={"decision": decision}, headers=headers
    )


async def status(env: Env, approval_id: str) -> dict[str, Any]:
    result = await env.call("gateway_approval_status", {"approval_id": approval_id})
    data: dict[str, Any] = result["result"]["structuredContent"]
    return data


async def test_proposing_changes_nothing_and_says_so(env: Env) -> None:
    pending = await propose(env)
    assert pending["status"] == "awaiting_approval"
    assert pending["summary"] == "Give C0001's duty on 2026-12-03 to C0002. Reason: Sick <b>leave</b>"
    assert pending["approve_url"] == f"{BASE}/approvals/{pending['approval_id']}"
    assert "Not done yet" in pending["message"]
    assert (await status(env, pending["approval_id"]))["status"] == "pending"
    assert env.idp.exchange_calls == []  # not even a token exchange for the write yet


@pytest.mark.parametrize(
    ("make_token", "status_code", "reason"),
    [
        (lambda e: e.agent_token(), 403, "not_a_human_sign_in"),  # the agent tries to approve itself
        (lambda e: e.approval_token("erik"), 403, "wrong_user"),
        (lambda e: e.approval_token(signed_in_ago=600), 403, "stale_sign_in"),
        (lambda e: e.approval_token(scope="openid roster.read"), 403, "missing_scope"),
    ],
)
async def test_who_cannot_approve(env: Env, make_token: Any, status_code: int, reason: str) -> None:
    pending = await propose(env)
    r = await decide(env, pending["approval_id"], make_token(env))
    assert (r.status_code, r.json()["error"]) == (status_code, reason)
    assert (await status(env, pending["approval_id"]))["status"] == "pending"


async def test_approval_needs_a_token_and_a_clear_decision(env: Env) -> None:
    pending = await propose(env)
    assert (await decide(env, pending["approval_id"], None)).status_code == 401
    r = await env.http.post(
        f"/approvals/{pending['approval_id']}/decision",
        content=b"not json",
        headers={"authorization": f"Bearer {env.approval_token()}"},
    )
    assert r.status_code == 400
    assert (await decide(env, pending["approval_id"], env.approval_token(), "maybe")).status_code == 400


async def test_approved_swap_runs_once_as_the_person(env: Env) -> None:
    pending = await propose(env)
    r = await decide(env, pending["approval_id"], env.approval_token())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "executed" and body["result"]["status"] == "pending_approval"
    assert body["result"]["swap_id"].startswith("S")
    # the partner saw the person, through the approval app, not the agent
    assert (
        env.idp.exchange_calls[-1]["scope"] == "roster.read swap.write"
        or "swap.write" in env.idp.exchange_calls[-1]["scope"]
    )
    after = await status(env, pending["approval_id"])
    assert after["status"] == "executed" and after["result"]["swap_id"] == body["result"]["swap_id"]
    again = await decide(env, pending["approval_id"], env.approval_token())
    assert (again.status_code, again.json()["error"]) == (409, "already_decided")


async def test_rejection(env: Env) -> None:
    pending = await propose(env)
    r = await decide(env, pending["approval_id"], env.approval_token(), "reject")
    assert r.json()["status"] == "rejected"
    assert (await status(env, pending["approval_id"]))["status"] == "rejected"


async def test_requests_expire(env: Env) -> None:
    pending = await propose(env)
    env.clock.t += 601
    assert (await status(env, pending["approval_id"]))["status"] == "expired"
    r = await decide(env, pending["approval_id"], env.approval_token())
    assert (r.status_code, r.json()["error"]) == (410, "approval_expired")


async def test_partner_still_decides_after_approval(env: Env) -> None:
    """Erik approves his own request, but his role can't swap: the partner says no."""
    pending = await propose(env, "erik")
    r = await decide(env, pending["approval_id"], env.approval_token("erik"))
    assert (r.status_code, r.json()["status"]) == (502, "failed")
    assert r.json()["error"] == "partner_forbidden"


async def test_every_step_is_in_the_audit_chain(env: Env) -> None:
    pending = await propose(env)
    await decide(env, pending["approval_id"], env.agent_token())  # refused
    await decide(env, pending["approval_id"], env.approval_token())  # approved
    import json

    records = [json.loads(line) for line in env.audit.lines]
    swap = [
        (r["decision"], r["reason"].split(":")[0], r["client_id"])
        for r in records
        if r["tool"] == "crew.propose_swap"
    ]
    assert swap == [
        ("pending_approval", "approval", "crewgate-mcp-client"),
        ("denied", "not_a_human_sign_in", "crewgate-mcp-client"),
        ("allowed", "approved", "crewgate-approvals"),
    ]
    assert verify_chain(env.audit.lines, env.gw.settings.audit_key).ok


# ---- the browser flow ---------------------------------------------------------------------


async def test_page_shows_the_summary_escaped_with_strict_headers(env: Env) -> None:
    pending = await propose(env)
    page = await env.http.get(f"/approvals/{pending['approval_id']}")
    assert page.status_code == 200
    assert "Sick &lt;b&gt;leave&lt;/b&gt;" in page.text and "<b>leave" not in page.text
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY" and page.headers["cache-control"] == "no-store"
    assert (await env.http.get("/approvals/nope-not-real")).status_code == 404


async def test_browser_flow_forces_a_fresh_sign_in(env: Env) -> None:
    pending = await propose(env)
    start = await env.http.get(f"/approvals/{pending['approval_id']}/start", params={"decision": "approve"})
    assert start.status_code == 303
    url = urlsplit(start.headers["location"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "idp.test"
    assert q["client_id"] == "crewgate-approvals" and q["prompt"] == "login" and q["max_age"] == "0"
    assert q["code_challenge_method"] == "S256" and q["redirect_uri"] == f"{BASE}/approvals/callback"
    assert q["scope"] == "openid swap.write"

    env.idp.auth_codes["code-1"] = {
        "sub": "user-anna",
        "azp": "crewgate-approvals",
        "scope": "openid swap.write",
        "auth_time": int(env.clock()),
    }
    done = await env.http.get("/approvals/callback", params={"code": "code-1", "state": q["state"]})
    assert done.status_code == 200 and "Done" in done.text and "executed" in done.text
    token_call = env.idp.token_requests[-2]  # the code exchange (the last one is the OBO exchange)
    assert token_call.headers["authorization"].startswith("Basic ")  # confidential client
    assert "code_verifier=" in token_call.content.decode()

    replay = await env.http.get("/approvals/callback", params={"code": "code-1", "state": q["state"]})
    assert replay.status_code == 400  # state is single use


async def test_browser_flow_edge_cases(env: Env) -> None:
    pending = await propose(env)
    aid = pending["approval_id"]
    assert (await env.http.get(f"/approvals/{aid}/start", params={"decision": "yes"})).status_code == 400
    assert (await env.http.get("/approvals/nope/start", params={"decision": "approve"})).status_code == 404
    assert (
        await env.http.get("/approvals/callback", params={"code": "x", "state": "unknown"})
    ).status_code == 400

    def state() -> str:
        return ""

    start = await env.http.get(f"/approvals/{aid}/start", params={"decision": "reject"})
    st = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    cancelled = await env.http.get("/approvals/callback", params={"error": "access_denied", "state": st})
    assert cancelled.status_code == 400 and "Nothing was changed" in cancelled.text

    start = await env.http.get(f"/approvals/{aid}/start", params={"decision": "approve"})
    st = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    bad_code = await env.http.get("/approvals/callback", params={"code": "never-issued", "state": st})
    assert bad_code.status_code == 400 and "Sign-in failed" in bad_code.text

    start = await env.http.get(f"/approvals/{aid}/start", params={"decision": "reject"})
    st = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    env.idp.auth_codes["c2"] = {"sub": "user-anna", "azp": "crewgate-approvals", "scope": "openid swap.write",
                                "auth_time": int(env.clock())}  # fmt: skip
    rejected = await env.http.get("/approvals/callback", params={"code": "c2", "state": st})
    assert "Rejected" in rejected.text
    assert (await env.http.get(f"/approvals/{aid}")).status_code == 409
    assert (await env.http.get(f"/approvals/{aid}/start", params={"decision": "approve"})).status_code == 409
