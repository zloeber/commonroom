from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from commonroom.capabilities import has_capability
from commonroom.engine import WorkspaceEngine
from commonroom.errors import CommonroomError, InvalidRequest, LimitExceeded, Unauthorized
from commonroom.gui import INGRESS_PAGE, OWNER_PAGE
from commonroom.protocol import discovery_document, protocol_document
from commonroom.skill import load_skill

logger = logging.getLogger("commonroom")


def create_app(
    engine: WorkspaceEngine,
    *,
    access: str = "session",
    host_session: dict[str, Any] | None = None,
) -> FastAPI:
    """HTTP adapter over the workspace engine.

    ``access="owner"`` is the loopback human projection. It sets a cookie and
    still requires that cookie or a bearer session. ``access="session"`` is
    the ingress any transport plugin forwards to. Neither mode treats a
    transport endpoint as authorization.
    """
    if access not in {"owner", "session"}:
        raise ValueError("access must be 'owner' or 'session'")
    app = FastAPI(title="Commonroom", version="0.1.0")
    app.state.engine = engine
    app.state.access = access
    app.state.host_session = host_session
    app.state.rate_buckets = defaultdict(list)

    def principal(request: Request) -> dict[str, Any]:
        _limit(request)
        token = _bearer(request) or request.cookies.get("commonroom_session")
        if not token:
            raise Unauthorized("session required")
        return engine.authenticate(token)

    def _limit(request: Request) -> None:
        cap = engine.limits.max_requests_per_minute
        if cap <= 0:
            return
        key = _bearer(request) or request.client.host if request.client else "anon"
        now = time.monotonic()
        hits = [stamp for stamp in app.state.rate_buckets[key] if now - stamp < 60]
        if len(hits) >= cap:
            raise LimitExceeded("rate limit exceeded", retry_after_seconds=5)
        hits.append(now)
        app.state.rate_buckets[key] = hits

    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > engine.limits.max_request_bytes:
            return JSONResponse(
                status_code=413,
                content={"error": "limit_exceeded", "message": "request too large"},
            )
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except CommonroomError as exc:
            response = JSONResponse(status_code=exc.http_status, content=exc.to_dict())
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "operation=%s result=%s duration_ms=%s",
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response

    @app.exception_handler(CommonroomError)
    async def handle_commonroom_error(_request: Request, exc: CommonroomError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.exception_handler(ValueError)
    async def handle_value_error(_request: Request, exc: ValueError) -> JSONResponse:
        if isinstance(exc, CommonroomError):
            return JSONResponse(status_code=exc.http_status, content=exc.to_dict())
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request", "message": str(exc)},
        )

    @app.get("/.well-known/agent-workspace")
    def well_known() -> dict[str, Any]:
        return discovery_document(engine.workspace_id)

    @app.get("/protocol")
    def protocol() -> dict[str, Any]:
        return protocol_document()

    @app.get("/skills")
    def skills() -> dict[str, Any]:
        return {
            "skills": [
                {
                    "name": "commonroom",
                    "description": "Join a room, observe it, lease a region, and propose changes.",
                    "path": "/skills/commonroom",
                }
            ]
        }

    @app.get("/skills/commonroom")
    def skill() -> PlainTextResponse:
        return PlainTextResponse(load_skill())

    @app.post("/session/join")
    def session_join(payload: dict[str, Any]) -> dict[str, Any]:
        token = payload.get("invite") or payload.get("invite_token")
        if not token:
            raise Unauthorized("invitation required")
        kind = payload.get("kind", "agent")
        if kind not in {"human", "agent"}:
            raise InvalidRequest("kind must be 'human' or 'agent'")
        transport_name = str(payload.get("transport") or "remote")[:64]
        return engine.join(
            kind=kind,
            name=str(payload.get("name") or "participant")[:80],
            invite_token=str(token),
            transport=transport_name,
        )

    @app.get("/workspace/status")
    def workspace_status(request: Request) -> dict[str, Any]:
        session = principal(request)
        engine.require(session, "read")
        return _redact_status(engine.status(), session)

    @app.get("/workspace/summary")
    def workspace_summary(
        request: Request,
        max_lines: int = 20,
        max_bytes: int = 2048,
        max_depth: int | None = None,
    ) -> dict[str, Any]:
        session = principal(request)
        engine.require(session, "read")
        return engine.summary(max_lines=max_lines, max_bytes=max_bytes, max_depth=max_depth)

    @app.get("/workspace/regions")
    def workspace_regions(request: Request) -> list[dict[str, Any]]:
        session = principal(request)
        engine.require(session, "read")
        return engine.regions()

    @app.get("/workspace/read")
    def workspace_read(
        request: Request,
        section: str | None = None,
        max_lines: int | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        session = principal(request)
        engine.require(session, "read")
        return engine.read(section=section, max_lines=max_lines, max_bytes=max_bytes)

    @app.get("/workspace/diff")
    def workspace_diff(request: Request, base_version: int) -> dict[str, Any]:
        session = principal(request)
        engine.require(session, "read")
        return engine.diff(base_version)

    @app.get("/workspace/changes_since")
    def changes_since(
        request: Request, version: int, limit: int = 20, max_items: int | None = None
    ) -> dict[str, Any]:
        session = principal(request)
        engine.require(session, "read")
        return engine.changes_since(version, limit=limit, max_items=max_items)

    @app.get("/workspace/history")
    def workspace_history(
        request: Request, limit: int = 50, max_items: int | None = None
    ) -> list[dict[str, Any]]:
        session = principal(request)
        engine.require(session, "read")
        return engine.history(limit=limit, max_items=max_items)

    @app.get("/workspace/participants")
    def participants(request: Request) -> list[dict[str, Any]]:
        session = principal(request)
        engine.require(session, "read")
        return engine.participants()

    @app.get("/workspace/leases")
    def leases(request: Request) -> list[dict[str, Any]]:
        session = principal(request)
        engine.require(session, "read")
        return engine.leases()

    @app.get("/workspace/proposals")
    def proposals(request: Request) -> list[dict[str, Any]]:
        session = principal(request)
        engine.require(session, "read")
        return engine.proposals()

    @app.get("/workspace/diagnostics")
    def diagnostics(request: Request) -> dict[str, Any]:
        session = principal(request)
        engine.require(session, "read")
        return engine.connection_diagnostics()

    @app.get("/workspace/room")
    def room(request: Request) -> dict[str, Any]:
        session = principal(request)
        engine.require(session, "read")
        payload: dict[str, Any] = {
            "status": _redact_status(engine.status(), session),
            "participants": engine.participants(),
            "leases": engine.leases(),
            "proposals": engine.proposals(),
            "events": engine.history(limit=20),
            "summary": engine.summary(),
            "diagnostics": engine.connection_diagnostics(),
        }
        if has_capability(session["capabilities"], "invite"):
            payload["invites"] = engine.list_invites()
        return payload

    @app.post("/workspace/checkout")
    def checkout(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        session = principal(request)
        if "target" not in payload:
            raise InvalidRequest("target is required")
        minutes = payload.get("minutes")
        return engine.checkout(
            session["participant_id"],
            str(payload["target"]),
            minutes=int(minutes) if minutes is not None else None,
            session=session,
        )

    @app.post("/workspace/renew")
    def renew(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        session = principal(request)
        if "lease_id" not in payload:
            raise InvalidRequest("lease_id is required")
        minutes = payload.get("minutes")
        return engine.renew(
            str(payload["lease_id"]),
            session["participant_id"],
            minutes=int(minutes) if minutes is not None else None,
            session=session,
        )

    @app.post("/workspace/release")
    def release(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        session = principal(request)
        engine.require(session, "checkout")
        if "lease_id" not in payload:
            raise InvalidRequest("lease_id is required")
        engine.release(
            str(payload["lease_id"]),
            participant_id=session["participant_id"],
            admin=has_capability(session["capabilities"], "admin"),
        )
        return {"status": "ok"}

    @app.post("/workspace/propose")
    def propose(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        session = principal(request)
        required = {"target", "base_version", "summary", "change"}
        if not required.issubset(payload):
            raise InvalidRequest("missing required proposal fields")
        return engine.propose(
            participant_id=session["participant_id"],
            target=str(payload["target"]),
            base_version=int(payload["base_version"]),
            summary=str(payload["summary"]),
            change=payload["change"],
            session=session,
        )

    @app.post("/workspace/proposal/review")
    def review(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        session = principal(request)
        if "proposal_id" not in payload or "approve" not in payload:
            raise InvalidRequest("missing proposal review fields")
        return engine.review_proposal(
            proposal_id=str(payload["proposal_id"]),
            reviewer_id=session["participant_id"],
            approve=bool(payload["approve"]),
            session=session,
        )

    @app.post("/workspace/commit")
    def commit(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        session = principal(request)
        if "proposal_id" in payload:
            return engine.commit_proposal(
                str(payload["proposal_id"]),
                session["participant_id"],
                session=session,
            )
        required = {"base_version", "summary", "content"}
        if not required.issubset(payload):
            raise InvalidRequest("missing required commit fields")
        return engine.commit(
            participant_id=session["participant_id"],
            base_version=int(payload["base_version"]),
            summary=str(payload["summary"]),
            content=str(payload["content"]),
            target=str(payload.get("target", "workspace")),
            session=session,
        )

    @app.post("/workspace/invite")
    def invite(request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        session = principal(request)
        engine.require(session, "invite")
        body = payload or {}
        capabilities = body.get("capabilities")
        return engine.create_invite(
            permissions=body.get("permissions"),
            target=body.get("target"),
            expires_in_minutes=int(
                body.get("expires_in_minutes", engine.limits.default_invite_minutes)
            ),
            max_uses=body.get("max_uses", 1),
            capabilities=capabilities,
        )

    @app.get("/workspace/invites")
    def invites(request: Request) -> list[dict[str, Any]]:
        session = principal(request)
        engine.require(session, "invite")
        return engine.list_invites()

    @app.post("/workspace/invite/revoke")
    def invite_revoke(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        session = principal(request)
        engine.require(session, "invite")
        engine.revoke_invite(token=payload.get("token"), invite_id=payload.get("invite_id"))
        return {"status": "ok"}

    @app.post("/workspace/leave")
    def leave(request: Request) -> dict[str, Any]:
        session = principal(request)
        engine.leave(session["participant_id"])
        return {"status": "ok"}

    @app.get("/workspace/observe")
    def observe(
        request: Request,
        since_event_id: int = 0,
        since_version: int | None = None,
        poll_seconds: float = 1.0,
        max_polls: int | None = None,
    ):
        session = principal(request)
        engine.require(session, "observe")

        def stream():
            last_id = since_event_id
            polls = 0
            while max_polls is None or polls < max_polls:
                events = engine.events_since(last_id, since_version=since_version)
                if events:
                    for event in events:
                        last_id = int(event["event_id"])
                        yield f"data: {json.dumps(event)}\n\n"
                else:
                    yield ": keepalive\n\n"
                polls += 1
                if max_polls is not None and polls >= max_polls:
                    break
                time.sleep(max(0.05, min(poll_seconds, 5.0)))

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        if access == "owner" and host_session is not None:
            response = HTMLResponse(OWNER_PAGE)
            response.set_cookie(
                "commonroom_session",
                host_session["session_id"],
                httponly=True,
                samesite="strict",
                path="/",
            )
            return response
        return HTMLResponse(INGRESS_PAGE)

    return app


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header.split(" ", 1)[1].strip()
    return None


def _redact_status(status: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    if has_capability(session["capabilities"], "admin"):
        return status
    redacted = dict(status)
    redacted["path"] = None
    return redacted
