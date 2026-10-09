"""Real Tailcat dial. Not part of `task check`.

Set COMMONROOM_TAILCAT_SMOKE=1 and run this file when the tailcat binary
and a network path are available. Two processes on one machine still share
a network stack; the cross-machine procedure is docs/remote-acceptance.md.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import httpx
import pytest

from commonroom.cli import main
from commonroom.engine import WorkspaceEngine
from commonroom.protocol import decode_invitation
from commonroom.serving import start_room

pytestmark = pytest.mark.skipif(
    os.environ.get("COMMONROOM_TAILCAT_SMOKE") != "1",
    reason="set COMMONROOM_TAILCAT_SMOKE=1 to dial Tailcat",
)


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_tailcat_cli_join_propose_and_commit(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "idea.md"
    workspace.write_text("# Architecture\n\n## Goals\n\nship\n", encoding="utf-8")
    engine = WorkspaceEngine(workspace)
    room = start_room(engine, port=_port(), name="Zach", transport_name="tailcat")
    try:
        listener = room.snapshot()["listener"]
        assert listener["type"] == "tailcat"
        assert listener["encrypted"] is True
        assert "address" not in listener
        document = decode_invitation(room.invite["uri"])
        assert document["transport"]["type"] == "tailcat"
        assert str(document["transport"]["address"]).startswith("tc")
        assert "127.0.0.1" not in json.dumps(document["transport"])

        uri = room.invite["uri"]
        assert (
            main(["join", uri, "--name", "Agent B", "--kind", "agent", "--json", "--no-hold"]) == 0
        )
        joined = json.loads(capsys.readouterr().out)
        assert joined["workspace_id"] == document["workspace_id"]
        assert joined["transport"] == "tailcat"
        session = joined["session_id"]

        assert main(["call", uri, "GET", "/workspace/summary", "--session", session]) == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["status"] == 200
        assert summary["body"]["version"] == 1

        assert (
            main(
                [
                    "call",
                    uri,
                    "POST",
                    "/workspace/propose",
                    "--session",
                    session,
                    "--body",
                    json.dumps(
                        {
                            "target": "section:Goals",
                            "base_version": 1,
                            "summary": "name the outcome",
                            "change": {
                                "section": "Goals",
                                "content": "## Goals\n\nship together\n",
                            },
                        }
                    ),
                ]
            )
            == 0
        )
        proposal_id = json.loads(capsys.readouterr().out)["body"]["proposal_id"]
        assert any(
            person["name"] == "Agent B" and person["transport"] == "tailcat"
            for person in engine.participants()
        )

        with httpx.Client(base_url=room.local_url) as human:
            assert human.get("/").status_code == 200
            approved = human.post(
                "/workspace/proposal/review",
                json={"proposal_id": proposal_id, "approve": True},
            )
            assert approved.status_code == 200

        assert (
            main(
                [
                    "call",
                    uri,
                    "POST",
                    "/workspace/commit",
                    "--session",
                    session,
                    "--body",
                    json.dumps({"proposal_id": proposal_id}),
                ]
            )
            == 0
        )
        committed = json.loads(capsys.readouterr().out)
        assert committed["status"] == 200
        assert committed["body"]["version"] == 2
        assert "ship together" in workspace.read_text(encoding="utf-8")

        # New dial, same session. This is reconnect; it does not redeem again.
        assert main(["call", uri, "GET", "/workspace/status", "--session", session]) == 0
        status = json.loads(capsys.readouterr().out)
        assert status["status"] == 200
        assert status["body"]["path"] is None
        assert status["body"]["version"] == 2
    finally:
        room.shutdown()
        engine.close()
