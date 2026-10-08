from __future__ import annotations

import socket
from pathlib import Path

import httpx

from commonroom.client import http_exchange
from commonroom.engine import WorkspaceEngine
from commonroom.protocol import INVITATION_PREFIX, decode_invitation
from commonroom.serving import render_banner, start_room
from commonroom.transport.loopback import LoopbackTransport
from commonroom.transport.registry import connect_invitation


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _exchange(
    document: dict, method: str, path: str, body: dict | None = None, session: str | None = None
):
    plugin, connection, _hint = connect_invitation(document)
    headers = {"Authorization": f"Bearer {session}"} if session else None
    try:
        return http_exchange(connection, method, path, json_body=body, headers=headers)
    finally:
        connection.close()
        plugin.close()


def test_remote_participant_over_the_transport(tmp_path: Path) -> None:
    workspace = tmp_path / "idea.md"
    workspace.write_text("# Architecture\n\n## Goals\n\nship\n", encoding="utf-8")
    engine = WorkspaceEngine(workspace)
    room = start_room(
        engine,
        port=_port(),
        name="Zach",
        transport=LoopbackTransport(),
    )
    try:
        banner = room.banner()
        assert banner.startswith("Commonroom\n")
        assert "tailcat://" not in banner
        assert INVITATION_PREFIX in banner
        document = decode_invitation(room.invite["uri"])
        assert document["transports"][0]["type"] == "loopback"
        transport_line = next(line for line in banner.splitlines() if "loopback" in line)
        assert str(document["transport"]["port"]) not in transport_line

        status, joined = _exchange(
            document,
            "POST",
            "/session/join",
            {
                "invite": document["secret"],
                "name": "Agent A",
                "kind": "agent",
                "transport": "loopback",
            },
        )
        assert status == 200
        session = joined["session_id"]

        status, summary = _exchange(document, "GET", "/workspace/summary", session=session)
        assert status == 200
        assert summary["version"] == 1

        status, lease = _exchange(
            document,
            "POST",
            "/workspace/checkout",
            {"target": "section:Goals"},
            session=session,
        )
        assert status == 200
        assert lease["target"] == "section:Goals"

        status, proposal = _exchange(
            document,
            "POST",
            "/workspace/propose",
            {
                "target": "section:Goals",
                "base_version": summary["version"],
                "summary": "name the outcome",
                "change": {"section": "Goals", "content": "## Goals\n\nship together\n"},
            },
            session=session,
        )
        assert status == 200

        with httpx.Client(base_url=room.local_url) as human:
            assert human.get("/").status_code == 200
            approved = human.post(
                "/workspace/proposal/review",
                json={"proposal_id": proposal["proposal_id"], "approve": True},
            )
            assert approved.status_code == 200

        status, history = _exchange(document, "GET", "/workspace/history?limit=20", session=session)
        assert status == 200
        assert any(event["event_type"] == "proposal.approved" for event in history)

        status, committed = _exchange(
            document,
            "POST",
            "/workspace/commit",
            {"proposal_id": proposal["proposal_id"]},
            session=session,
        )
        assert status == 200
        text = workspace.read_text(encoding="utf-8")
        assert "ship together" in text
        assert text.startswith("# Architecture")

        status, again = _exchange(
            document,
            "GET",
            "/workspace/status",
            session=session,
        )
        assert again["version"] == committed["version"]
        assert again["path"] is None

        engine.revoke_invite(token=room.invite["token"])
        status, denied = _exchange(
            document,
            "POST",
            "/session/join",
            {"invite": document["secret"], "name": "Other", "kind": "agent"},
        )
        assert status == 403
        assert denied["error"] == "invitation_revoked"

        db_path = engine.db_path
        version = engine.current_version
    finally:
        room.shutdown()
        engine.close()

    reloaded = WorkspaceEngine(workspace, db_path=db_path)
    try:
        assert reloaded.current_version == version
        assert "ship together" in workspace.read_text(encoding="utf-8")
    finally:
        reloaded.close()


def test_banner_hides_the_endpoint() -> None:
    text = render_banner(
        {
            "name": "idea",
            "version": 3,
            "lifecycle": "ACTIVE",
            "local_url": "http://127.0.0.1:8000",
            "invite": "commonroom://join/abc",
            "listener": {"type": "loopback", "mode": "local", "encrypted": False},
            "participants": [{"kind": "human", "name": "Zach"}],
        }
    )
    assert "commonroom://join/abc" in text
    assert "Zach" in text
    assert "tailcat://" not in text
