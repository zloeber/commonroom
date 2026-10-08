from __future__ import annotations

import os
from pathlib import Path

_FALLBACK = """# Commonroom

The Commonroom skill file was not installed with this package.
Discover the live protocol at `/.well-known/agent-workspace`, `/protocol`, and `/skills`.
Join with the invitation, then observe, checkout, propose, and commit only after review.
"""


def _candidates() -> list[Path]:
    paths: list[Path] = []
    override = os.environ.get("COMMONROOM_SKILL")
    if override:
        paths.append(Path(override))
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        paths.append(parent / "skills" / "commonroom" / "SKILL.md")
        paths.append(parent / "data" / "SKILL.md")
    paths.append(Path.cwd() / "skills" / "commonroom" / "SKILL.md")
    return paths


def load_skill() -> str:
    for path in _candidates():
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return _FALLBACK
