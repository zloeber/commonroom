from __future__ import annotations

from pathlib import Path

import pytest

from commonroom.engine import WorkspaceConflictError, WorkspaceEngine


def _engine(tmp_path: Path, content: str = "# Architecture\ninitial\n") -> WorkspaceEngine:
    workspace = tmp_path / "workspace.md"
    workspace.write_text(content, encoding="utf-8")
    return WorkspaceEngine(workspace)


def test_workspace_create_read_modify_and_restart(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    agent = engine.join(kind="agent", name="agent-a")

    before = engine.status()
    assert before["version"] == 1

    commit = engine.commit(
        participant_id=agent["participant_id"],
        base_version=1,
        summary="update architecture",
        content="# Architecture\nupdated\n",
    )
    assert commit["version"] == 2
    assert "updated" in engine.read()["content"]

    db_path = engine.db_path
    ws_path = engine.workspace_path
    engine.close()

    reloaded = WorkspaceEngine(ws_path, db_path=db_path)
    assert reloaded.status()["version"] == 2
    assert "updated" in reloaded.read()["content"]


def test_lease_visibility_between_participants(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    a = engine.join(kind="agent", name="a")
    b = engine.join(kind="human", name="b")

    lease = engine.checkout(a["participant_id"], "section:Architecture")
    leases = engine.leases()
    assert leases[0]["lease_id"] == lease["lease_id"]

    with pytest.raises(ValueError):
        engine.checkout(b["participant_id"], "section:Architecture")


def test_stale_commit_returns_structured_conflict(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    a = engine.join(kind="agent", name="a")
    b = engine.join(kind="agent", name="b")

    engine.commit(a["participant_id"], 1, "first", "# Architecture\none\n")

    with pytest.raises(WorkspaceConflictError) as exc:
        engine.commit(b["participant_id"], 1, "stale", "# Architecture\ntwo\n")

    payload = exc.value.payload
    assert payload["error"] == "stale_version"
    assert payload["current_version"] == 2
    assert "attempted@v1" in payload["diff"]


def test_proposal_review_and_commit(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    agent = engine.join(kind="agent", name="writer")
    human = engine.join(kind="human", name="reviewer")

    proposal = engine.propose(
        participant_id=agent["participant_id"],
        target="section:Architecture",
        base_version=1,
        summary="refine architecture section",
        change={"content": "# Architecture\nrefined\n"},
    )
    assert proposal["status"] == "pending"

    review = engine.review_proposal(proposal["proposal_id"], human["participant_id"], approve=True)
    assert review["status"] == "approved"

    commit = engine.commit_proposal(proposal["proposal_id"], human["participant_id"])
    assert commit["version"] == 2
    assert "refined" in engine.read()["content"]


def test_invite_expired_and_revoked(tmp_path: Path) -> None:
    engine = _engine(tmp_path)

    expired = engine.create_invite(expires_in_minutes=-1)
    with pytest.raises(ValueError, match="expired"):
        engine.join(kind="agent", name="late", invite_token=expired["token"])

    valid = engine.create_invite(expires_in_minutes=10)
    engine.revoke_invite(valid["token"])
    with pytest.raises(ValueError, match="revoked"):
        engine.join(kind="agent", name="blocked", invite_token=valid["token"])


def test_summary_and_changes_since_are_bounded(tmp_path: Path) -> None:
    engine = _engine(tmp_path, content="# Architecture\nline1\nline2\nline3\n")
    agent = engine.join(kind="agent", name="a")

    engine.commit(
        agent["participant_id"], 1, "change", "# Architecture\nline1\nline2 updated\nline3\n"
    )

    summary = engine.summary(max_lines=2, max_bytes=20)
    assert len(summary["excerpt"].splitlines()) <= 2
    assert len(summary["excerpt"].encode("utf-8")) <= 20

    changes = engine.changes_since(1, limit=1)
    assert changes["to_version"] == 2
    assert len(changes["changes"]) == 1
