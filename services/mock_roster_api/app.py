"""Mock crew-roster partner API.

Behaves like a strict third-party API so the crew plugin is tested against real problems:
- OAuth2 client-credentials token endpoint issuing short-lived JWTs with scopes
- scope checks per endpoint (roster.read, swap.write)
- per-client rate limiting that returns 429 with Retry-After
- cursor pagination with a capped page size
The clock is injectable so tests can move time without sleeping.
"""

import base64
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Annotated, Any

import jwt
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .data import build_dataset
from .settings import ApiSettings

Claims = dict[str, Any]


class RateLimitedError(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after


class SlidingWindowLimiter:
    def __init__(self, limit: int, window: float, clock: Callable[[], float]) -> None:
        self.limit, self.window, self.clock = limit, window, clock
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = self.clock()
        q = self.hits[key]
        while q and now - q[0] >= self.window:
            q.popleft()
        if len(q) >= self.limit:
            raise RateLimitedError(max(1, int(self.window - (now - q[0])) + 1))
        q.append(now)


class SwapRequest(BaseModel):
    requester_id: str
    partner_id: str
    date: str


def _encode_cursor(idx: int) -> str:
    return base64.urlsafe_b64encode(str(idx).encode()).decode()


def _decode_cursor(cursor: str) -> int:
    try:
        return int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "invalid cursor") from exc


def create_app(settings: ApiSettings | None = None, clock: Callable[[], float] = time.time) -> FastAPI:
    settings = settings or ApiSettings()
    app = FastAPI(title="Mock Crew Roster API")
    crew, roster = build_dataset()
    crew_by_id = {c["crew_id"]: c for c in crew}
    swaps: dict[str, dict[str, Any]] = {}
    limiter = SlidingWindowLimiter(settings.rate_limit_per_minute, 60.0, clock)
    key = settings.jwt_signing_key.get_secret_value()

    @app.exception_handler(RateLimitedError)
    async def _rate_limited(_: Request, exc: RateLimitedError) -> JSONResponse:
        return JSONResponse(
            {"detail": "rate_limited"}, status_code=429, headers={"Retry-After": str(exc.retry_after)}
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/oauth/token")
    async def token(
        grant_type: Annotated[str, Form()],
        client_id: Annotated[str, Form()],
        client_secret: Annotated[str, Form()],
        scope: Annotated[str, Form()] = "",
    ) -> dict[str, Any]:
        if grant_type != "client_credentials":
            raise HTTPException(400, "unsupported_grant_type")
        entry = settings.clients.get(client_id)
        if not entry or entry[0] != client_secret:
            raise HTTPException(401, "invalid_client")
        allowed = set(entry[1])
        requested = set(scope.split()) if scope else allowed
        if not requested <= allowed:
            raise HTTPException(400, "invalid_scope")
        now = int(clock())
        claims = {
            "sub": client_id,
            "aud": settings.audience,
            "iat": now,
            "exp": now + settings.token_ttl_seconds,
            "scope": " ".join(sorted(requested)),
            "jti": uuid.uuid4().hex,
        }
        return {
            "access_token": jwt.encode(claims, key, algorithm="HS256"),
            "token_type": "Bearer",  # nosec B105 token type name, not a secret
            "expires_in": settings.token_ttl_seconds,
            "scope": claims["scope"],
        }

    def require_scope(needed: str) -> Callable[..., Any]:
        async def dep(authorization: Annotated[str | None, Header()] = None) -> Claims:
            if not authorization or not authorization.startswith("Bearer "):
                raise HTTPException(401, "missing_token", headers={"WWW-Authenticate": "Bearer"})
            try:
                claims: Claims = jwt.decode(
                    authorization[7:],
                    key,
                    algorithms=["HS256"],
                    audience=settings.audience,
                    options={"verify_exp": False, "verify_iat": False, "verify_nbf": False},
                )
            except jwt.PyJWTError as exc:
                raise HTTPException(401, "invalid_token", headers={"WWW-Authenticate": "Bearer"}) from exc
            if claims["exp"] <= clock():
                raise HTTPException(401, "token_expired", headers={"WWW-Authenticate": "Bearer"})
            if needed not in str(claims.get("scope", "")).split():
                raise HTTPException(403, "insufficient_scope")
            limiter.check(str(claims["sub"]))
            return claims

        return dep

    @app.get("/crew")
    async def list_crew(
        _: Annotated[Claims, Depends(require_scope("roster.read"))],
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1)] = 25,
    ) -> dict[str, Any]:
        limit = min(limit, settings.page_size_max)
        start = _decode_cursor(cursor) if cursor else 0
        nxt = start + limit
        return {
            "items": crew[start:nxt],
            "next_cursor": _encode_cursor(nxt) if nxt < len(crew) else None,
        }

    @app.get("/crew/{crew_id}/roster")
    async def get_roster(
        crew_id: str, _: Annotated[Claims, Depends(require_scope("roster.read"))]
    ) -> dict[str, Any]:
        if crew_id not in roster:
            raise HTTPException(404, "crew_not_found")
        return {"crew_id": crew_id, "days": roster[crew_id]}

    @app.post("/swaps", status_code=201)
    async def create_swap(
        body: SwapRequest, claims: Annotated[Claims, Depends(require_scope("swap.write"))]
    ) -> dict[str, Any]:
        for cid in (body.requester_id, body.partner_id):
            if cid not in crew_by_id:
                raise HTTPException(404, f"crew_not_found: {cid}")
        sid = f"S{len(swaps) + 1:05d}"
        swaps[sid] = {
            "swap_id": sid,
            **body.model_dump(),
            "status": "pending_approval",
            "requested_by": claims["sub"],
        }
        return swaps[sid]

    return app


app = create_app()
