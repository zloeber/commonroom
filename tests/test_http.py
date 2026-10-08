from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from commonroom.engine import WorkspaceEngine
from commonroom.http import create_app


def test_http_adapter_matches_engine_status(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace.md"
    workspace.write_text("# Roadmap\ninitial\n", encoding="utf-8")
    engine = WorkspaceEngine(workspace)

    client = TestClient(create_app(engine))
    http_status = client.get("/workspace/status")

    assert http_status.status_code == 200
    assert http_status.json()["workspace_id"] == engine.status()["workspace_id"]
    assert http_status.json()["version"] == engine.status()["version"]


def test_well_known_and_protocol_discovery(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace.md"
    workspace.write_text("# Plan\n", encoding="utf-8")
    client = TestClient(create_app(WorkspaceEngine(workspace)))

    wk = client.get("/.well-known/agent-workspace")
    protocol = client.get("/protocol")
    skills = client.get("/skills")

    assert wk.status_code == 200
    assert "workspace.status" in wk.json()["operations"]
    assert protocol.status_code == 200
    assert "workspace" in protocol.json()
    assert skills.status_code == 200
    assert skills.json()["skills"][0]["name"] == "commonroom"
