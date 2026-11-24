"""Step 7 demo: sign in to the web console like a browser would, and ask the agent something.

    uv run python scripts/demo_web.py                          # make up: console on :8090
    uv run python scripts/demo_web.py --base http://localhost:8443 --edge   # behind the edge

Checks on the way: the cookie is an opaque id (no token in the browser), cross-site POSTs are
refused, and, with --edge, that /metrics and Keycloak's admin console are not public.
"""

import argparse
import asyncio
import sys

import httpx

from crewgate.clientauth.devlogin import submit_login_form

QUESTION = "Who could cover a captain at Gothenburg on 2026-12-05?"


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://localhost:8090")
    p.add_argument("--edge", action="store_true", help="also check what the edge must not expose")
    a = p.parse_args()
    base = a.base.rstrip("/")
    async with (
        httpx.AsyncClient(base_url=base, trust_env=False, timeout=60) as web,
        httpx.AsyncClient(trust_env=False, timeout=60) as idp,
    ):
        r = await web.get("/login")
        page = await idp.get(r.headers["location"])
        answer = await submit_login_form(idp, page, "anna", "dev-only-anna")  # the realm's dev-only test user
        r = await web.get(answer.headers["location"].removeprefix(base))
        session = (await web.get("/api/session")).json()
        cookie = web.cookies.get("crewgate_session") or ""
        opaque = bool(cookie) and "." not in cookie
        print(f"1. signed in as {session.get('username')}; cookie is an opaque id: {opaque}")

        r = await web.post(
            "/api/chat", json={"message": "hi"}, headers={"origin": "https://evil.example", "x-crewgate": "1"}
        )
        print(f"2. cross-site chat request -> HTTP {r.status_code}")

        if a.edge:
            for path in ("/metrics", "/auth/admin/", "/auth/realms/master/.well-known/openid-configuration"):
                r = await web.get(path)
                print(f"3. {path} through the edge -> HTTP {r.status_code}")
            r = await web.get("/.well-known/oauth-protected-resource/mcp")
            print(f"4. MCP metadata through the edge -> {r.json().get('authorization_servers')}")

        if not session.get("chat_enabled"):
            print("5. chat is off on this server (no model key); sign-in path checked")
            return 0
        r = await web.post(
            "/api/chat", json={"message": QUESTION}, headers={"origin": base, "x-crewgate": "1"}
        )
        if r.status_code != 200:
            print(f"5. chat -> HTTP {r.status_code} {r.json().get('error')}")
            return 0 if r.status_code in (429, 503) else 1  # a busy free tier is not a code problem
        turn = r.json()
        tools = [s["tool"] for s in turn["steps"] if s["kind"] == "tool"]
        print(f"5. agent used {tools}; guard ok: {turn['guard']['ok']}")
        print(f"   reply: {turn['reply'][:160]}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
