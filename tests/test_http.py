from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from commonroom.engine import WorkspaceEngine
from commonroom.http import create_app


def _owner(
    tmp_path: Path, content: str = "# Plan\n\nbody\n"
) -> tuple[WorkspaceEngine, TestClient, dict]:
    workspace = tmp_path / "workspace.md"
    workspace.write_text(content, encoding="utf-8")
    engine = WorkspaceEngine(workspace)
    host = engine.ensure_host("Zach")
    client = TestClient(create_app(engine, access="owner", host_session=host))
    return engine, client, host


def test_ingress_requires_a_session_and_discovery_is_public(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace.md"
    workspace.write_text("# Plan\n", encoding="utf-8")
    client = TestClient(create_app(WorkspaceEngine(workspace), access="session"))

    discovery = client.get("/.well-known/agent-workspace")
    assert discovery.status_code == 200
    assert "workspace.status" in discovery.json()["operations"]
    assert client.get("/protocol").status_code == 200
    assert client.get("/skills/commonroom").status_code == 200
    assert client.get("/workspace/status").status_code == 401
    assert client.get("/workspace/status").json()["error"] == "unauthorized"
    assert "session" not in client.get("/").text


def test_owner_cookie_is_not_in_the_page_and_can_review(tmp_path: Path) -> None:
    engine, client, host = _owner(tmp_path, "# Architecture\n\n## Goals\n\nship\n")
    page = client.get("/")
    assert page.status_code == 200
    assert host["session_id"] not in page.text
    assert "commonroom_session" in page.headers["set-cookie"]

    status = client.get("/workspace/status")
    assert status.status_code == 200
    assert status.json()["version"] == 1
    assert status.json()["path"] is not None

    room = client.get("/workspace/room")
    assert room.status_code == 200
    assert room.json()["status"]["name"] == "workspace"
    assert "address" not in room.json()["diagnostics"]["listener"]


def test_agent_cannot_bypass_review_or_escalate(tmp_path: Path) -> None:
    engine, owner, _host = _owner(tmp_path, "# Architecture\n\n## Goals\n\nship\n")
    engine.set_transport(
        {
            "type": "loopback",
            "address": "secret-endpoint",
            "port": 9,
            "encrypted": True,
            "mode": "private",
        }
    )
    invite = engine.create_invite(capabilities=["read", "observe", "propose", "checkout", "commit"])
    ingress = TestClient(create_app(engine, access="session"))
    joined = ingress.post(
        "/session/join",
        json={
            "invite": invite["token"],
            "name": "Agent A",
            "kind": "agent",
            "transport": "loopback",
        },
    )
    assert joined.status_code == 200
    assert joined.json()["capabilities"] == ["read", "observe", "propose", "checkout", "commit"]
    session = joined.json()["session_id"]
    headers = {"Authorization": f"Bearer {session}"}

    raw = ingress.post(
        "/workspace/commit",
        headers=headers,
        json={"base_version": 1, "summary": "overwrite", "content": "nope\n"},
    )
    assert raw.status_code == 403
    assert raw.json()["error"] == "permission_denied"

    public = ingress.get("/workspace/diagnostics", headers=headers)
    assert public.status_code == 200
    assert "secret-endpoint" not in public.text
    assert public.json()["listener"]["type"] == "loopback"
    assert public.json()["listener"]["encrypted"] is True

    proposed = ingress.post(
        "/workspace/propose",
        headers=headers,
        json={
            "target": "section:Goals",
            "base_version": 1,
            "summary": "sharpen the goal",
            "change": {"section": "Goals", "content": "## Goals\n\nship the room\n"},
        },
    )
    assert proposed.status_code == 200
    proposal_id = proposed.json()["proposal_id"]

    denied = ingress.post(
        "/workspace/proposal/review",
        headers=headers,
        json={"proposal_id": proposal_id, "approve": True},
    )
    assert denied.status_code == 403

    owner.get("/")
    approved = owner.post(
        "/workspace/proposal/review", json={"proposal_id": proposal_id, "approve": True}
    )
    assert approved.status_code == 200
    committed = ingress.post(
        "/workspace/commit", headers=headers, json={"proposal_id": proposal_id}
    )
    assert committed.status_code == 200
    assert "ship the room" in engine.workspace_path.read_text(encoding="utf-8")
    assert "# Architecture" in engine.workspace_path.read_text(encoding="utf-8")

    stale = ingress.post(
        "/workspace/propose",
        headers=headers,
        json={
            "target": "section:Goals",
            "base_version": 1,
            "summary": "too late",
            "change": {"content": "# Architecture\n\n## Goals\n\nlost\n"},
        },
    )
    owner.post(
        "/workspace/proposal/review",
        json={"proposal_id": stale.json()["proposal_id"], "approve": True},
    )
    conflict = ingress.post(
        "/workspace/commit",
        headers=headers,
        json={"proposal_id": stale.json()["proposal_id"]},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "stale_version"
    assert "current_version" in conflict.json()


def test_embedded_mount_stays_session_scoped(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace.md"
    workspace.write_text("# Embedded\n", encoding="utf-8")
    outer = FastAPI()
    outer.mount("/commonroom", create_app(WorkspaceEngine(workspace), access="session"))
    client = TestClient(outer)
    assert client.get("/commonroom/.well-known/agent-workspace").status_code == 200
    assert client.get("/commonroom/workspace/status").status_code == 401
