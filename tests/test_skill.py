from __future__ import annotations

from pathlib import Path

from commonroom.protocol import OPERATIONS
from commonroom.skill import load_skill


def test_skill_teaches_the_live_protocol() -> None:
    skill = load_skill()
    assert "commonroom://join/" in skill
    assert "session required" not in skill
    for name in (
        "workspace.status",
        "workspace.summary",
        "workspace.checkout",
        "workspace.propose",
        "workspace.commit",
        "stale_version",
    ):
        assert name in skill or name.split(".", 1)[1] in skill
    missing = [item["name"] for item in OPERATIONS if item["name"].split(".", 1)[1] not in skill]
    assert "checkout" not in missing
    assert "observe" in skill
    assert "lease_conflict" in skill
    repo_skill = Path("skills/commonroom/SKILL.md").read_text(encoding="utf-8")
    assert repo_skill == skill
