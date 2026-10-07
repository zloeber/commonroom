from __future__ import annotations

import json
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

from commonroom.engine import WorkspaceConflictError, WorkspaceEngine


def create_app(engine: WorkspaceEngine) -> FastAPI:
    app = FastAPI(title="Commonroom", version="0.1.0")

    @app.get("/.well-known/agent-workspace")
    def well_known() -> dict[str, Any]:
        return {
            "name": "Commonroom",
            "workspace_id": engine.workspace_id,
            "protocol": "/protocol",
            "skills": "/skills",
            "operations": [
                "workspace.status",
                "workspace.read",
                "workspace.diff",
                "workspace.observe",
                "workspace.checkout",
                "workspace.release",
                "workspace.propose",
                "workspace.commit",
                "workspace.participants",
                "workspace.history",
                "workspace.invite",
            ],
        }

    @app.get("/skills")
    def skills() -> dict[str, Any]:
        return {
            "skills": [
                {
                    "name": "commonroom",
                    "description": "Join a local-first workspace, observe state, use leases, and propose changes.",
                }
            ]
        }

    @app.get("/protocol")
    def protocol() -> dict[str, Any]:
        return {
            "workspace": {
                "status": "GET /workspace/status",
                "read": "GET /workspace/read",
                "diff": "GET /workspace/diff",
                "observe": "GET /workspace/observe",
                "checkout": "POST /workspace/checkout",
                "release": "POST /workspace/release",
                "propose": "POST /workspace/propose",
                "commit": "POST /workspace/commit",
                "participants": "GET /workspace/participants",
                "history": "GET /workspace/history",
                "invite": "POST /workspace/invite",
            }
        }

    @app.get("/workspace/status")
    def workspace_status() -> dict[str, Any]:
        return engine.status()

    @app.get("/workspace/read")
    def workspace_read(
        section: str | None = None,
        max_lines: int | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        try:
            return engine.read(section=section, max_lines=max_lines, max_bytes=max_bytes)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/workspace/summary")
    def workspace_summary(max_lines: int = 20, max_bytes: int = 2048) -> dict[str, Any]:
        return engine.summary(max_lines=max_lines, max_bytes=max_bytes)

    @app.get("/workspace/diff")
    def workspace_diff(base_version: int) -> dict[str, Any]:
        try:
            return engine.diff(base_version)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/workspace/history")
    def workspace_history(limit: int = 50) -> list[dict[str, Any]]:
        return engine.history(limit=limit)

    @app.get("/workspace/changes_since")
    def changes_since(version: int, limit: int = 20) -> dict[str, Any]:
        return engine.changes_since(version, limit=limit)

    @app.get("/workspace/participants")
    def participants() -> list[dict[str, Any]]:
        return engine.participants()

    @app.get("/workspace/leases")
    def leases() -> list[dict[str, Any]]:
        return engine.leases()

    @app.get("/workspace/proposals")
    def proposals() -> list[dict[str, Any]]:
        return engine.proposals()

    @app.post("/workspace/join")
    def join(payload: dict[str, Any]) -> dict[str, Any]:
        return engine.join(
            kind=payload.get("kind", "agent"),
            name=payload.get("name", "participant"),
            capabilities=payload.get("capabilities", []),
            invite_token=payload.get("invite_token"),
        )

    @app.post("/workspace/checkout")
    def checkout(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return engine.checkout(
                participant_id=payload["participant_id"],
                target=payload["target"],
                minutes=int(payload.get("minutes", 15)),
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/workspace/release")
    def release(payload: dict[str, Any]) -> dict[str, Any]:
        if "lease_id" not in payload:
            raise HTTPException(status_code=400, detail="lease_id is required")
        engine.release(payload["lease_id"])
        return {"status": "ok"}

    @app.post("/workspace/propose")
    def propose(payload: dict[str, Any]) -> dict[str, Any]:
        required = {"participant_id", "target", "base_version", "summary", "change"}
        if not required.issubset(payload):
            raise HTTPException(status_code=400, detail="missing required proposal fields")
        return engine.propose(
            participant_id=payload["participant_id"],
            target=payload["target"],
            base_version=int(payload["base_version"]),
            summary=payload["summary"],
            change=payload["change"],
        )

    @app.post("/workspace/proposal/review")
    def review(payload: dict[str, Any]) -> dict[str, Any]:
        required = {"proposal_id", "reviewer_id", "approve"}
        if not required.issubset(payload):
            raise HTTPException(status_code=400, detail="missing proposal review fields")
        return engine.review_proposal(
            proposal_id=payload["proposal_id"],
            reviewer_id=payload["reviewer_id"],
            approve=bool(payload["approve"]),
        )

    @app.post("/workspace/commit")
    def commit(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            if "proposal_id" in payload:
                return engine.commit_proposal(payload["proposal_id"], payload["participant_id"])
            return engine.commit(
                participant_id=payload["participant_id"],
                base_version=int(payload["base_version"]),
                summary=payload["summary"],
                content=payload["content"],
                target=payload.get("target", "workspace"),
            )
        except WorkspaceConflictError as exc:
            raise HTTPException(status_code=409, detail=exc.payload) from exc
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/workspace/invite")
    def invite(payload: dict[str, Any]) -> dict[str, Any]:
        return engine.create_invite(
            permissions=payload.get("permissions", "read_write"),
            target=payload.get("target"),
            expires_in_minutes=int(payload.get("expires_in_minutes", 60)),
            max_uses=payload.get("max_uses", 1),
        )

    @app.post("/workspace/invite/revoke")
    def invite_revoke(payload: dict[str, Any]) -> dict[str, Any]:
        token = payload.get("token")
        if not token:
            raise HTTPException(status_code=400, detail="token is required")
        engine.revoke_invite(token)
        return {"status": "ok"}

    @app.get("/workspace/observe")
    def observe(since_event_id: int = 0, poll_seconds: float = 1.0):
        def stream():
            last_id = since_event_id
            while True:
                events = [
                    event for event in engine.history(limit=100) if event["event_id"] > last_id
                ]
                if events:
                    for event in sorted(events, key=lambda item: item["event_id"]):
                        last_id = event["event_id"]
                        yield f"data: {json.dumps(event)}\n\n"
                time.sleep(max(0.1, min(poll_seconds, 5.0)))

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return """
<!doctype html>
<html>
<head><meta charset=\"utf-8\" /><title>Commonroom</title></head>
<body>
<h1>Commonroom</h1>
<pre id=\"status\"></pre>
<h2>Participants</h2><pre id=\"participants\"></pre>
<h2>Leases</h2><pre id=\"leases\"></pre>
<h2>Pending Proposals</h2><pre id=\"proposals\"></pre>
<h2>Recent Events</h2><pre id=\"events\"></pre>
<script>
async function load() {
  const [status, participants, leases, proposals, events] = await Promise.all([
    fetch('/workspace/status').then(r => r.json()),
    fetch('/workspace/participants').then(r => r.json()),
    fetch('/workspace/leases').then(r => r.json()),
    fetch('/workspace/proposals').then(r => r.json()),
    fetch('/workspace/history?limit=20').then(r => r.json()),
  ]);
  document.getElementById('status').textContent = JSON.stringify(status, null, 2);
  document.getElementById('participants').textContent = JSON.stringify(participants, null, 2);
  document.getElementById('leases').textContent = JSON.stringify(leases, null, 2);
  document.getElementById('proposals').textContent = JSON.stringify(proposals.filter(p => p.status === 'pending'), null, 2);
  document.getElementById('events').textContent = JSON.stringify(events, null, 2);
}
load();
setInterval(load, 3000);
</script>
</body>
</html>
        """

    return app
