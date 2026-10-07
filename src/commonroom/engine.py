from __future__ import annotations

import difflib
import hashlib
import json
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_LEASE_MINUTES = 15
MAX_LEASE_MINUTES = 120


class WorkspaceConflictError(Exception):
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        super().__init__("workspace version conflict")


class WorkspaceEngine:
    workspace_path: Path
    db_path: Path

    def __init__(self, workspace_path: str | Path, db_path: str | Path | None = None):
        self.workspace_path = Path(workspace_path).expanduser().resolve()
        if not self.workspace_path.exists():
            self.workspace_path.parent.mkdir(parents=True, exist_ok=True)
            self.workspace_path.write_text("", encoding="utf-8")
        if not self.workspace_path.is_file():
            raise ValueError("workspace_path must be a file")

        if db_path is None:
            db_path = self.workspace_path.with_suffix(self.workspace_path.suffix + ".commonroom.db")
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        self._bootstrap()

    def close(self) -> None:
        self._conn.close()

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()

    def _file_hash(self, content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    def _get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return str(row["value"])

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}_{secrets.token_hex(8)}"

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS versions (
                version INTEGER PRIMARY KEY,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                summary TEXT NOT NULL,
                participant_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS participants (
                participant_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                capabilities TEXT NOT NULL,
                joined_at TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS leases (
                lease_id TEXT PRIMARY KEY,
                participant_id TEXT NOT NULL,
                target TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT
            );
            CREATE TABLE IF NOT EXISTS proposals (
                proposal_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                participant_id TEXT NOT NULL,
                target TEXT NOT NULL,
                base_version INTEGER NOT NULL,
                summary TEXT NOT NULL,
                change_json TEXT NOT NULL,
                status TEXT NOT NULL,
                review_by TEXT,
                reviewed_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS invites (
                token TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                permissions TEXT NOT NULL,
                target TEXT,
                expires_at TEXT NOT NULL,
                max_uses INTEGER,
                used_count INTEGER NOT NULL DEFAULT 0,
                revoked_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def _bootstrap(self) -> None:
        workspace_id = self._get_meta("workspace_id")
        if workspace_id is None:
            workspace_id = self._next_id("workspace")
            content = self.workspace_path.read_text(encoding="utf-8")
            self._set_meta("workspace_id", workspace_id)
            self._set_meta("lifecycle", "ACTIVE")
            self._set_meta("current_version", "1")
            self._conn.execute(
                "INSERT INTO versions(version, content, content_hash, summary, participant_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (1, content, self._file_hash(content), "workspace created", None, self._now()),
            )
            self._record_event("workspace.created", {"workspace_id": workspace_id, "version": 1})
        self._conn.commit()

    def _record_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO events(event_type, payload_json, created_at) VALUES (?, ?, ?)",
            (event_type, json.dumps(payload), self._now()),
        )
        self._conn.commit()

    @property
    def workspace_id(self) -> str:
        workspace_id = self._get_meta("workspace_id")
        if workspace_id is None:
            raise RuntimeError("workspace_id missing")
        return workspace_id

    @property
    def current_version(self) -> int:
        return int(self._get_meta("current_version", "1"))

    def _read_current_content(self) -> str:
        row = self._conn.execute(
            "SELECT content FROM versions WHERE version = ?", (self.current_version,)
        ).fetchone()
        if row is None:
            return self.workspace_path.read_text(encoding="utf-8")
        return str(row["content"])

    def _heading_tree(self, content: str) -> list[dict[str, Any]]:
        tree: list[dict[str, Any]] = []
        for line_no, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                level = len(stripped) - len(stripped.lstrip("#"))
                title = stripped[level:].strip()
                if title:
                    tree.append({"title": title, "level": level, "line": line_no})
        return tree

    def _limit_text(
        self, text: str, max_lines: int | None = None, max_bytes: int | None = None
    ) -> str:
        output = text
        if max_lines is not None and max_lines > 0:
            output = "\n".join(output.splitlines()[:max_lines])
        if max_bytes is not None and max_bytes > 0:
            encoded = output.encode("utf-8")
            if len(encoded) > max_bytes:
                output = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return output

    def status(self) -> dict[str, Any]:
        content = self._read_current_content()
        return {
            "workspace_id": self.workspace_id,
            "lifecycle": self._get_meta("lifecycle", "ACTIVE"),
            "version": self.current_version,
            "substrate": "markdown",
            "path": str(self.workspace_path),
            "content_hash": self._file_hash(content),
            "participants": len(self.participants()),
            "active_leases": len(self.leases()),
            "pending_proposals": len([p for p in self.proposals() if p["status"] == "pending"]),
            "heading_tree": self._heading_tree(content),
        }

    def read(
        self,
        section: str | None = None,
        max_lines: int | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        content = self._read_current_content()
        selected = content
        if section:
            lines = content.splitlines()
            start_idx = None
            end_idx = len(lines)
            for idx, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("#") and stripped.lstrip("#").strip() == section:
                    start_idx = idx
                    start_level = len(stripped) - len(stripped.lstrip("#"))
                    for end_search in range(idx + 1, len(lines)):
                        candidate = lines[end_search].strip()
                        if candidate.startswith("#"):
                            level = len(candidate) - len(candidate.lstrip("#"))
                            if level <= start_level:
                                end_idx = end_search
                                break
                    break
            if start_idx is None:
                raise ValueError(f"section '{section}' not found")
            selected = "\n".join(lines[start_idx:end_idx])
        limited = self._limit_text(selected, max_lines=max_lines, max_bytes=max_bytes)
        return {
            "workspace_id": self.workspace_id,
            "version": self.current_version,
            "section": section,
            "content": limited,
        }

    def summary(self, max_lines: int = 20, max_bytes: int = 2048) -> dict[str, Any]:
        content = self._read_current_content()
        return {
            "workspace_id": self.workspace_id,
            "version": self.current_version,
            "outline": self._heading_tree(content)[:10],
            "excerpt": self._limit_text(content, max_lines=max_lines, max_bytes=max_bytes),
        }

    def diff(self, base_version: int) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT content FROM versions WHERE version = ?", (base_version,)
        ).fetchone()
        if row is None:
            raise ValueError("base version does not exist")
        old = str(row["content"]).splitlines(keepends=True)
        new = self._read_current_content().splitlines(keepends=True)
        output = "".join(
            difflib.unified_diff(
                old, new, fromfile=f"v{base_version}", tofile=f"v{self.current_version}"
            )
        )
        return {
            "workspace_id": self.workspace_id,
            "base_version": base_version,
            "current_version": self.current_version,
            "diff": output,
        }

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT event_id, event_type, payload_json, created_at FROM events ORDER BY event_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "event_id": int(row["event_id"]),
                "event_type": str(row["event_type"]),
                "payload": json.loads(str(row["payload_json"])),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def changes_since(self, version: int, limit: int = 20) -> dict[str, Any]:
        rows = self._conn.execute(
            "SELECT version, summary, participant_id, created_at FROM versions WHERE version > ? ORDER BY version ASC LIMIT ?",
            (version, limit),
        ).fetchall()
        return {
            "workspace_id": self.workspace_id,
            "from_version": version,
            "to_version": self.current_version,
            "changes": [
                {
                    "version": int(row["version"]),
                    "summary": str(row["summary"]),
                    "participant_id": row["participant_id"],
                    "created_at": str(row["created_at"]),
                }
                for row in rows
            ],
        }

    def participants(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT participant_id, kind, name, capabilities, joined_at, last_seen FROM participants ORDER BY joined_at ASC"
        ).fetchall()
        return [
            {
                "participant_id": str(row["participant_id"]),
                "kind": str(row["kind"]),
                "name": str(row["name"]),
                "capabilities": json.loads(str(row["capabilities"])),
                "joined_at": str(row["joined_at"]),
                "last_seen": str(row["last_seen"]),
            }
            for row in rows
        ]

    def join(
        self,
        kind: str,
        name: str,
        capabilities: list[str] | None = None,
        invite_token: str | None = None,
    ) -> dict[str, Any]:
        if kind not in {"human", "agent"}:
            raise ValueError("kind must be 'human' or 'agent'")
        if invite_token is not None:
            self._consume_invite(invite_token)

        participant_id = self._next_id("participant")
        now = self._now()
        self._conn.execute(
            "INSERT INTO participants(participant_id, kind, name, capabilities, joined_at, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
            (participant_id, kind, name, json.dumps(capabilities or []), now, now),
        )
        self._conn.commit()
        payload = {
            "participant_id": participant_id,
            "kind": kind,
            "name": name,
            "capabilities": capabilities or [],
        }
        self._record_event("participant.joined", payload)
        return payload

    def leave(self, participant_id: str) -> None:
        self._conn.execute("DELETE FROM participants WHERE participant_id = ?", (participant_id,))
        self._conn.commit()
        self._record_event("participant.left", {"participant_id": participant_id})

    def leases(self) -> list[dict[str, Any]]:
        now = self._now()
        self._conn.execute(
            "UPDATE leases SET released_at = COALESCE(released_at, ?) WHERE released_at IS NULL AND expires_at < ?",
            (now, now),
        )
        self._conn.commit()
        rows = self._conn.execute(
            "SELECT lease_id, participant_id, target, acquired_at, expires_at FROM leases WHERE released_at IS NULL ORDER BY acquired_at ASC"
        ).fetchall()
        return [
            {
                "lease_id": str(row["lease_id"]),
                "participant_id": str(row["participant_id"]),
                "target": str(row["target"]),
                "acquired_at": str(row["acquired_at"]),
                "expires_at": str(row["expires_at"]),
            }
            for row in rows
        ]

    def checkout(
        self, participant_id: str, target: str, minutes: int = DEFAULT_LEASE_MINUTES
    ) -> dict[str, Any]:
        if minutes > MAX_LEASE_MINUTES:
            raise ValueError(f"lease duration exceeds max ({MAX_LEASE_MINUTES} minutes)")

        now_dt = datetime.now(UTC)
        expires_at = (now_dt + timedelta(minutes=minutes)).isoformat()

        existing = self._conn.execute(
            "SELECT lease_id FROM leases WHERE target = ? AND released_at IS NULL AND expires_at >= ?",
            (target, now_dt.isoformat()),
        ).fetchone()
        if existing is not None:
            raise ValueError("target is already leased")

        lease_id = self._next_id("lease")
        self._conn.execute(
            "INSERT INTO leases(lease_id, participant_id, target, acquired_at, expires_at, released_at) VALUES (?, ?, ?, ?, ?, NULL)",
            (lease_id, participant_id, target, now_dt.isoformat(), expires_at),
        )
        self._conn.commit()
        payload = {
            "lease_id": lease_id,
            "participant_id": participant_id,
            "target": target,
            "expires_at": expires_at,
        }
        self._record_event("lease.acquired", payload)
        return payload

    def release(self, lease_id: str) -> None:
        self._conn.execute(
            "UPDATE leases SET released_at = ? WHERE lease_id = ? AND released_at IS NULL",
            (self._now(), lease_id),
        )
        self._conn.commit()
        self._record_event("lease.released", {"lease_id": lease_id})

    def proposals(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT proposal_id, workspace_id, participant_id, target, base_version, summary, change_json, status, created_at FROM proposals ORDER BY created_at DESC"
        ).fetchall()
        return [
            {
                "proposal_id": str(row["proposal_id"]),
                "workspace_id": str(row["workspace_id"]),
                "participant_id": str(row["participant_id"]),
                "target": str(row["target"]),
                "base_version": int(row["base_version"]),
                "summary": str(row["summary"]),
                "change": json.loads(str(row["change_json"])),
                "status": str(row["status"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def propose(
        self,
        participant_id: str,
        target: str,
        base_version: int,
        summary: str,
        change: dict[str, Any],
    ) -> dict[str, Any]:
        proposal_id = self._next_id("proposal")
        payload = {
            "proposal_id": proposal_id,
            "workspace_id": self.workspace_id,
            "participant_id": participant_id,
            "target": target,
            "base_version": base_version,
            "summary": summary,
            "change": change,
            "status": "pending",
            "created_at": self._now(),
        }
        self._conn.execute(
            "INSERT INTO proposals(proposal_id, workspace_id, participant_id, target, base_version, summary, change_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                proposal_id,
                self.workspace_id,
                participant_id,
                target,
                base_version,
                summary,
                json.dumps(change),
                "pending",
                payload["created_at"],
            ),
        )
        self._conn.commit()
        self._record_event("proposal.created", payload)
        return payload

    def review_proposal(self, proposal_id: str, reviewer_id: str, approve: bool) -> dict[str, Any]:
        status = "approved" if approve else "rejected"
        self._conn.execute(
            "UPDATE proposals SET status = ?, review_by = ?, reviewed_at = ? WHERE proposal_id = ?",
            (status, reviewer_id, self._now(), proposal_id),
        )
        self._conn.commit()
        payload = {"proposal_id": proposal_id, "reviewer_id": reviewer_id, "status": status}
        self._record_event(f"proposal.{status}", payload)
        return payload

    def _commit_content(
        self, participant_id: str | None, base_version: int, summary: str, content: str
    ) -> dict[str, Any]:
        current_content = self._read_current_content()
        if base_version != self.current_version:
            diff = "".join(
                difflib.unified_diff(
                    current_content.splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile=f"v{self.current_version}",
                    tofile=f"attempted@v{base_version}",
                )
            )
            raise WorkspaceConflictError(
                {
                    "error": "version_conflict",
                    "base_version": base_version,
                    "current_version": self.current_version,
                    "diff": diff,
                }
            )

        new_version = self.current_version + 1
        content_hash = self._file_hash(content)
        self.workspace_path.write_text(content, encoding="utf-8")
        self._conn.execute(
            "INSERT INTO versions(version, content, content_hash, summary, participant_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (new_version, content, content_hash, summary, participant_id, self._now()),
        )
        self._set_meta("current_version", str(new_version))
        payload = {
            "workspace_id": self.workspace_id,
            "version": new_version,
            "summary": summary,
            "participant_id": participant_id,
        }
        self._record_event("change.committed", payload)
        return payload

    def commit(
        self,
        participant_id: str,
        base_version: int,
        summary: str,
        content: str,
        target: str = "workspace",
    ) -> dict[str, Any]:
        payload = self._commit_content(participant_id, base_version, summary, content)
        payload["target"] = target
        return payload

    def commit_proposal(self, proposal_id: str, participant_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT base_version, summary, change_json, status FROM proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise ValueError("proposal not found")
        if str(row["status"]) != "approved":
            raise ValueError("proposal must be approved before commit")
        change = json.loads(str(row["change_json"]))
        content = change.get("content")
        if not isinstance(content, str):
            raise ValueError("proposal change must contain string field 'content'")
        payload = self._commit_content(
            participant_id, int(row["base_version"]), str(row["summary"]), content
        )
        self._conn.execute(
            "UPDATE proposals SET status = 'committed' WHERE proposal_id = ?",
            (proposal_id,),
        )
        self._conn.commit()
        self._record_event(
            "proposal.committed", {"proposal_id": proposal_id, "version": payload["version"]}
        )
        return payload

    def create_invite(
        self,
        permissions: str = "read_write",
        target: str | None = None,
        expires_in_minutes: int = 60,
        max_uses: int | None = 1,
    ) -> dict[str, Any]:
        token = secrets.token_urlsafe(24)
        expires_at = (datetime.now(UTC) + timedelta(minutes=expires_in_minutes)).isoformat()
        self._conn.execute(
            "INSERT INTO invites(token, workspace_id, permissions, target, expires_at, max_uses, used_count, revoked_at, created_at) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?)",
            (token, self.workspace_id, permissions, target, expires_at, max_uses, self._now()),
        )
        self._conn.commit()
        payload = {
            "token": token,
            "workspace_id": self.workspace_id,
            "permissions": permissions,
            "target": target,
            "expires_at": expires_at,
            "max_uses": max_uses,
        }
        self._record_event("invite.created", payload)
        return payload

    def revoke_invite(self, token: str) -> None:
        self._conn.execute(
            "UPDATE invites SET revoked_at = ? WHERE token = ?", (self._now(), token)
        )
        self._conn.commit()
        self._record_event("invite.revoked", {"token": token})

    def _consume_invite(self, token: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT token, permissions, target, expires_at, max_uses, used_count, revoked_at FROM invites WHERE token = ?",
            (token,),
        ).fetchone()
        if row is None:
            raise ValueError("invite not found")
        if row["revoked_at"] is not None:
            raise ValueError("invite revoked")
        if datetime.fromisoformat(str(row["expires_at"])) < datetime.now(UTC):
            raise ValueError("invite expired")
        max_uses = row["max_uses"]
        used_count = int(row["used_count"])
        if max_uses is not None and used_count >= int(max_uses):
            raise ValueError("invite usage limit reached")
        self._conn.execute(
            "UPDATE invites SET used_count = used_count + 1 WHERE token = ?", (token,)
        )
        self._conn.commit()
        return {
            "token": token,
            "permissions": str(row["permissions"]),
            "target": row["target"],
        }
