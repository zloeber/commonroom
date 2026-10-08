from __future__ import annotations

import difflib
import hashlib
import json
import os
import secrets
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any

from commonroom.capabilities import (
    capabilities_from_permissions,
    has_capability,
    normalize_capabilities,
)
from commonroom.errors import (
    InvalidRegion,
    InvalidRequest,
    InvitationExpired,
    InvitationRevoked,
    LeaseConflict,
    LimitExceeded,
    PermissionDenied,
    ProposalConflict,
    ProposalNotFound,
    SessionExpired,
    StaleVersion,
    Unauthorized,
    VersionNotFound,
)
from commonroom.errors import (
    WorkspaceConflictError as WorkspaceConflictError,
)
from commonroom.limits import Limits
from commonroom.protocol import encode_invitation

Clock = Callable[[], datetime]


def locked(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def wrapper(self: WorkspaceEngine, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class WorkspaceEngine:
    """Collaboration state for one user-owned substrate.

    The Markdown file is the document. SQLite stores versions, sessions,
    leases, proposals, invitations, and events. In-process callers are
    trusted. HTTP adapters pass a session so capabilities are enforced.
    """

    workspace_path: Path
    db_path: Path

    def __init__(
        self,
        workspace_path: str | Path,
        db_path: str | Path | None = None,
        *,
        limits: Limits | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.limits = limits or Limits()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
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
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=3000")
        self._init_schema()
        self._bootstrap()
        self._recover()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _now(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None:
            return current.replace(tzinfo=UTC)
        return current

    def _now_text(self) -> str:
        return self._now().isoformat()

    def _parse_time(self, value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed

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

    def _add_column(self, table: str, column: str, definition: str) -> None:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        names = {str(row["name"]) for row in rows}
        if column not in names:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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
                last_seen TEXT NOT NULL,
                left_at TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                participant_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                capabilities TEXT NOT NULL,
                transport TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                ended_at TEXT
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
                created_at TEXT NOT NULL,
                capabilities TEXT,
                invite_id TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self._add_column("participants", "left_at", "TEXT")
        self._add_column("invites", "capabilities", "TEXT")
        self._add_column("invites", "invite_id", "TEXT")
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
                (1, content, self._file_hash(content), "workspace created", None, self._now_text()),
            )
            self._record_event("workspace.created", {"version": 1})
        self._conn.commit()

    def _recover(self) -> None:
        self._expire_sessions()
        self._expire_leases()

    def _record_event(self, event_type: str, payload: dict[str, Any]) -> None:
        body = {"workspace_id": self.workspace_id, **payload}
        self._conn.execute(
            "INSERT INTO events(event_type, payload_json, created_at) VALUES (?, ?, ?)",
            (event_type, json.dumps(body), self._now_text()),
        )
        self._conn.execute(
            """
            DELETE FROM events WHERE event_id NOT IN (
                SELECT event_id FROM events ORDER BY event_id DESC LIMIT ?
            )
            """,
            (self.limits.max_events,),
        )
        self._conn.commit()

    def require(self, session: dict[str, Any], capability: str) -> None:
        granted = session.get("capabilities") or []
        if not isinstance(granted, list) or not has_capability(granted, capability):
            raise PermissionDenied(
                f"session is missing the {capability} capability",
                required=capability,
            )

    @property
    def workspace_id(self) -> str:
        workspace_id = self._get_meta("workspace_id")
        if workspace_id is None:
            raise RuntimeError("workspace_id missing")
        return workspace_id

    @property
    def current_version(self) -> int:
        return int(self._get_meta("current_version", "1") or "1")

    def _stored_content(self) -> str:
        row = self._conn.execute(
            "SELECT content FROM versions WHERE version = ?", (self.current_version,)
        ).fetchone()
        if row is None:
            return ""
        return str(row["content"])

    def _read_current_content(self) -> str:
        self._sync_substrate()
        return self._stored_content()

    def _sync_substrate(self) -> None:
        if not self.workspace_path.is_file():
            return
        disk = self.workspace_path.read_text(encoding="utf-8")
        if disk == self._stored_content():
            return
        self._append_version(
            participant_id=None,
            summary="external substrate edit",
            content=disk,
            write_file=False,
        )

    def _append_version(
        self,
        *,
        participant_id: str | None,
        summary: str,
        content: str,
        write_file: bool,
    ) -> dict[str, Any]:
        new_version = self.current_version + 1
        if write_file:
            self.workspace_path.write_text(content, encoding="utf-8")
        self._conn.execute(
            "INSERT INTO versions(version, content, content_hash, summary, participant_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                new_version,
                content,
                self._file_hash(content),
                summary,
                participant_id,
                self._now_text(),
            ),
        )
        self._set_meta("current_version", str(new_version))
        payload = {
            "version": new_version,
            "summary": summary,
            "participant_id": participant_id,
        }
        self._record_event("change.committed", payload)
        return payload

    def _headings(self, content: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        stack: list[tuple[int, str]] = []
        for index, line in enumerate(content.splitlines()):
            stripped = line.strip()
            if not stripped.startswith("#"):
                continue
            level = len(stripped) - len(stripped.lstrip("#"))
            if level < 1 or level > 6:
                continue
            title = stripped[level:].strip()
            if not title:
                continue
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            found.append(
                {
                    "title": title,
                    "level": level,
                    "line": index + 1,
                    "index": index,
                    "path": "/".join(item[1] for item in stack),
                }
            )
        return found

    def _span(self, content: str, section: str) -> tuple[int, int] | None:
        lines = content.splitlines()
        headings = self._headings(content)
        match = None
        for heading in headings:
            names = {
                heading["title"],
                heading["path"],
                f"section:{heading['title']}",
                f"section:{heading['path']}",
            }
            if section in names:
                match = heading
                break
        if match is None:
            return None
        end = len(lines)
        for heading in headings:
            if heading["index"] <= match["index"]:
                continue
            if heading["level"] <= match["level"]:
                end = heading["index"]
                break
        return match["index"], end

    def _validate_target(self, target: str) -> None:
        if target in {"workspace", "*"}:
            return
        if self._span(self._stored_content(), target) is None:
            raise InvalidRegion(f"section '{target}' not found", region=target)

    def _replace_section(self, content: str, section: str, replacement: str) -> str:
        span = self._span(content, section)
        if span is None:
            raise InvalidRegion(f"section '{section}' not found", region=section)
        lines = content.splitlines()
        start, end = span
        new_lines = lines[:start] + replacement.splitlines() + lines[end:]
        trailing = "\n" if content.endswith("\n") or replacement.endswith("\n") else ""
        return "\n".join(new_lines) + trailing

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

    def _bound_limit(self, limit: int, max_items: int | None = None) -> int:
        chosen = limit
        if max_items is not None:
            chosen = min(chosen, max_items)
        return max(1, min(chosen, self.limits.max_list_items))

    @locked
    def mark_started(self) -> None:
        self._set_meta("lifecycle", "ACTIVE")
        self._record_event("workspace.started", {"version": self.current_version})

    @locked
    def mark_stopped(self) -> None:
        self._record_event("workspace.stopped", {"version": self.current_version})

    @locked
    def set_transport(self, description: dict[str, Any] | None) -> None:
        if description is None:
            self._set_meta("transport_json", "")
            return
        payload = {key: value for key, value in description.items() if key != "pid"}
        payload["pid"] = os.getpid()
        self._set_meta("transport_json", json.dumps(payload))

    @locked
    def current_transport(self) -> dict[str, Any] | None:
        raw = self._get_meta("transport_json") or ""
        if not raw:
            return None
        data = json.loads(raw)
        pid = data.get("pid")
        if isinstance(pid, int) and not _pid_alive(pid):
            return None
        return {key: value for key, value in data.items() if key != "pid"}

    @locked
    def status(self) -> dict[str, Any]:
        content = self._read_current_content()
        return {
            "workspace_id": self.workspace_id,
            "lifecycle": self._get_meta("lifecycle", "ACTIVE"),
            "version": self.current_version,
            "substrate": "markdown",
            "path": str(self.workspace_path),
            "name": self.workspace_path.stem,
            "content_hash": self._file_hash(content),
            "participants": len(self.participants()),
            "active_leases": len(self.leases()),
            "pending_proposals": len(
                [item for item in self.proposals() if item["status"] == "pending"]
            ),
            "heading_tree": self.regions(),
        }

    @locked
    def regions(self) -> list[dict[str, Any]]:
        content = self._read_current_content()
        return [
            {
                "region": item["path"],
                "title": item["title"],
                "level": item["level"],
                "line": item["line"],
                "path": item["path"],
            }
            for item in self._headings(content)
        ]

    @locked
    def read(
        self,
        section: str | None = None,
        max_lines: int | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        content = self._read_current_content()
        selected = content
        if section:
            span = self._span(content, section)
            if span is None:
                raise InvalidRegion(f"section '{section}' not found", region=section)
            selected = "\n".join(content.splitlines()[span[0] : span[1]])
        limited = self._limit_text(selected, max_lines=max_lines, max_bytes=max_bytes)
        return {
            "workspace_id": self.workspace_id,
            "version": self.current_version,
            "section": section,
            "content": limited,
            "truncated": limited != selected,
        }

    @locked
    def summary(
        self,
        max_lines: int = 20,
        max_bytes: int = 2048,
        max_depth: int | None = None,
    ) -> dict[str, Any]:
        content = self._read_current_content()
        outline = self._headings(content)
        if max_depth is not None:
            outline = [item for item in outline if item["level"] <= max_depth]
        return {
            "workspace_id": self.workspace_id,
            "version": self.current_version,
            "outline": [
                {
                    "title": item["title"],
                    "level": item["level"],
                    "line": item["line"],
                    "path": item["path"],
                }
                for item in outline[:10]
            ],
            "excerpt": self._limit_text(content, max_lines=max_lines, max_bytes=max_bytes),
        }

    @locked
    def diff(self, base_version: int) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT content FROM versions WHERE version = ?", (base_version,)
        ).fetchone()
        if row is None:
            raise VersionNotFound("base version does not exist", base_version=base_version)
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

    @locked
    def history(self, limit: int = 50, max_items: int | None = None) -> list[dict[str, Any]]:
        chosen = self._bound_limit(limit, max_items)
        rows = self._conn.execute(
            "SELECT event_id, event_type, payload_json, created_at FROM events ORDER BY event_id DESC LIMIT ?",
            (chosen,),
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

    @locked
    def events_since(
        self,
        since_event_id: int = 0,
        since_version: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        chosen = self._bound_limit(limit)
        rows = self._conn.execute(
            """
            SELECT event_id, event_type, payload_json, created_at
            FROM events WHERE event_id > ? ORDER BY event_id ASC LIMIT ?
            """,
            (since_event_id, chosen),
        ).fetchall()
        events = [
            {
                "event_id": int(row["event_id"]),
                "event_type": str(row["event_type"]),
                "payload": json.loads(str(row["payload_json"])),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]
        if since_version is None:
            return events
        selected = []
        for event in events:
            version = event["payload"].get("version")
            if isinstance(version, int) and version <= since_version:
                continue
            selected.append(event)
        return selected

    @locked
    def changes_since(
        self, version: int, limit: int = 20, max_items: int | None = None
    ) -> dict[str, Any]:
        chosen = self._bound_limit(limit, max_items)
        rows = self._conn.execute(
            "SELECT version, summary, participant_id, created_at FROM versions WHERE version > ? ORDER BY version ASC LIMIT ?",
            (version, chosen),
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

    @locked
    def participants(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT participant_id, kind, name, capabilities, joined_at, last_seen,
                (
                    SELECT transport FROM sessions
                    WHERE sessions.participant_id = participants.participant_id
                      AND sessions.ended_at IS NULL
                    ORDER BY issued_at DESC LIMIT 1
                ) AS transport
            FROM participants
            WHERE left_at IS NULL
            ORDER BY joined_at ASC
            """
        ).fetchall()
        return [
            {
                "participant_id": str(row["participant_id"]),
                "kind": str(row["kind"]),
                "name": str(row["name"]),
                "capabilities": json.loads(str(row["capabilities"])),
                "joined_at": str(row["joined_at"]),
                "last_seen": str(row["last_seen"]),
                "transport": row["transport"],
            }
            for row in rows
        ]

    def _active_session_count(self) -> int:
        self._expire_sessions()
        row = self._conn.execute(
            "SELECT COUNT(*) AS count FROM sessions WHERE ended_at IS NULL"
        ).fetchone()
        return int(row["count"]) if row is not None else 0

    def _issue_session(
        self,
        *,
        participant_id: str,
        kind: str,
        name: str,
        capabilities: list[str],
        transport: str,
        session_minutes: int | None = None,
    ) -> dict[str, Any]:
        if self._active_session_count() >= self.limits.max_active_sessions:
            raise LimitExceeded("too many active sessions")
        minutes = session_minutes or self.limits.default_session_minutes
        if minutes > self.limits.max_session_minutes:
            raise LimitExceeded(
                f"session duration exceeds max ({self.limits.max_session_minutes} minutes)"
            )
        now = self._now()
        session_id = f"ses_{secrets.token_urlsafe(24)}"
        expires_at = (now + timedelta(minutes=minutes)).isoformat()
        issued_at = now.isoformat()
        self._conn.execute(
            """
            INSERT INTO sessions(
                session_id, participant_id, workspace_id, capabilities, transport,
                issued_at, expires_at, last_seen, ended_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                session_id,
                participant_id,
                self.workspace_id,
                json.dumps(capabilities),
                transport,
                issued_at,
                expires_at,
                issued_at,
            ),
        )
        self._conn.commit()
        self._record_event(
            "session.created",
            {
                "participant_id": participant_id,
                "transport": transport,
                "expires_at": expires_at,
            },
        )
        return {
            "session_id": session_id,
            "participant_id": participant_id,
            "workspace_id": self.workspace_id,
            "kind": kind,
            "name": name,
            "capabilities": capabilities,
            "transport": transport,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "last_seen": issued_at,
        }

    @locked
    def ensure_host(self, name: str, transport: str = "local") -> dict[str, Any]:
        row = self._conn.execute(
            """
            SELECT participant_id FROM participants
            WHERE kind = 'human' AND name = ? AND left_at IS NULL
            ORDER BY joined_at ASC LIMIT 1
            """,
            (name,),
        ).fetchone()
        now = self._now_text()
        if row is None:
            participant_id = self._next_id("participant")
            self._conn.execute(
                """
                INSERT INTO participants(participant_id, kind, name, capabilities, joined_at, last_seen, left_at)
                VALUES (?, 'human', ?, ?, ?, ?, NULL)
                """,
                (participant_id, name, json.dumps(["*"]), now, now),
            )
            self._conn.commit()
            self._record_event(
                "participant.joined",
                {
                    "participant_id": participant_id,
                    "kind": "human",
                    "name": name,
                    "capabilities": ["*"],
                },
            )
        else:
            participant_id = str(row["participant_id"])
        self._conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE participant_id = ? AND ended_at IS NULL",
            (now, participant_id),
        )
        self._conn.commit()
        return self._issue_session(
            participant_id=participant_id,
            kind="human",
            name=name,
            capabilities=["*"],
            transport=transport,
        )

    @locked
    def join(
        self,
        kind: str,
        name: str,
        capabilities: list[str] | None = None,
        invite_token: str | None = None,
        transport: str = "local",
        session_minutes: int | None = None,
    ) -> dict[str, Any]:
        if kind not in {"human", "agent"}:
            raise InvalidRequest("kind must be 'human' or 'agent'")
        granted = normalize_capabilities(capabilities)
        if invite_token is not None:
            invite = self._consume_invite(invite_token)
            granted = invite["capabilities"]
        participant_id = self._next_id("participant")
        now = self._now_text()
        self._conn.execute(
            """
            INSERT INTO participants(participant_id, kind, name, capabilities, joined_at, last_seen, left_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (participant_id, kind, name, json.dumps(granted), now, now),
        )
        self._conn.commit()
        self._record_event(
            "participant.joined",
            {
                "participant_id": participant_id,
                "kind": kind,
                "name": name,
                "capabilities": granted,
                "transport": transport,
            },
        )
        return self._issue_session(
            participant_id=participant_id,
            kind=kind,
            name=name,
            capabilities=granted,
            transport=transport[:64],
            session_minutes=session_minutes,
        )

    @locked
    def authenticate(self, session_id: str) -> dict[str, Any]:
        self._expire_sessions()
        row = self._conn.execute(
            """
            SELECT session_id, participant_id, workspace_id, capabilities, transport,
                   issued_at, expires_at, last_seen, ended_at
            FROM sessions WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None or row["ended_at"] is not None:
            raise Unauthorized("session not found")
        if self._parse_time(str(row["expires_at"])) <= self._now():
            self._conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE session_id = ?",
                (self._now_text(), session_id),
            )
            self._conn.commit()
            self._record_event("session.expired", {"participant_id": str(row["participant_id"])})
            raise SessionExpired()
        now = self._now_text()
        self._conn.execute(
            "UPDATE sessions SET last_seen = ? WHERE session_id = ?",
            (now, session_id),
        )
        self._conn.execute(
            "UPDATE participants SET last_seen = ? WHERE participant_id = ?",
            (now, row["participant_id"]),
        )
        self._conn.commit()
        participant = self._conn.execute(
            "SELECT kind, name FROM participants WHERE participant_id = ?",
            (row["participant_id"],),
        ).fetchone()
        return {
            "session_id": str(row["session_id"]),
            "participant_id": str(row["participant_id"]),
            "workspace_id": str(row["workspace_id"]),
            "kind": str(participant["kind"]) if participant else "agent",
            "name": str(participant["name"]) if participant else "participant",
            "capabilities": json.loads(str(row["capabilities"])),
            "transport": str(row["transport"]),
            "issued_at": str(row["issued_at"]),
            "expires_at": str(row["expires_at"]),
            "last_seen": now,
        }

    def _expire_sessions(self) -> None:
        now = self._now()
        rows = self._conn.execute(
            "SELECT session_id, participant_id, expires_at FROM sessions WHERE ended_at IS NULL"
        ).fetchall()
        for row in rows:
            if self._parse_time(str(row["expires_at"])) > now:
                continue
            self._conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE session_id = ? AND ended_at IS NULL",
                (now.isoformat(), row["session_id"]),
            )
            self._record_event("session.expired", {"participant_id": str(row["participant_id"])})

    @locked
    def leave(self, participant_id: str) -> None:
        now = self._now_text()
        self._conn.execute(
            "UPDATE participants SET left_at = ? WHERE participant_id = ? AND left_at IS NULL",
            (now, participant_id),
        )
        self._conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE participant_id = ? AND ended_at IS NULL",
            (now, participant_id),
        )
        leases = self._conn.execute(
            "SELECT lease_id FROM leases WHERE participant_id = ? AND released_at IS NULL",
            (participant_id,),
        ).fetchall()
        self._conn.commit()
        for lease in leases:
            self.release(str(lease["lease_id"]))
        self._record_event("participant.left", {"participant_id": participant_id})

    def _expire_leases(self) -> None:
        now = self._now()
        rows = self._conn.execute(
            """
            SELECT lease_id, participant_id, target, expires_at
            FROM leases WHERE released_at IS NULL
            """
        ).fetchall()
        for row in rows:
            if self._parse_time(str(row["expires_at"])) > now:
                continue
            self._conn.execute(
                "UPDATE leases SET released_at = ? WHERE lease_id = ? AND released_at IS NULL",
                (now.isoformat(), row["lease_id"]),
            )
            self._record_event(
                "lease.expired",
                {
                    "lease_id": str(row["lease_id"]),
                    "participant_id": str(row["participant_id"]),
                    "target": str(row["target"]),
                },
            )

    @locked
    def leases(self) -> list[dict[str, Any]]:
        self._expire_leases()
        rows = self._conn.execute(
            """
            SELECT lease_id, participant_id, target, acquired_at, expires_at
            FROM leases WHERE released_at IS NULL ORDER BY acquired_at ASC
            """
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

    @locked
    def checkout(
        self,
        participant_id: str,
        target: str,
        minutes: int | None = None,
        *,
        session: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if session is not None:
            self.require(session, "checkout")
            participant_id = str(session["participant_id"])
        duration = self.limits.default_lease_minutes if minutes is None else minutes
        if duration <= 0:
            raise InvalidRequest("lease duration must be positive")
        if duration > self.limits.max_lease_minutes:
            raise LimitExceeded(
                f"lease duration exceeds max ({self.limits.max_lease_minutes} minutes)"
            )
        self._sync_substrate()
        self._validate_target(target)
        self._expire_leases()
        now = self._now()
        existing = self._conn.execute(
            """
            SELECT lease_id, participant_id FROM leases
            WHERE target = ? AND released_at IS NULL AND expires_at >= ?
            """,
            (target, now.isoformat()),
        ).fetchone()
        if existing is not None:
            if str(existing["participant_id"]) == participant_id:
                return self.renew(str(existing["lease_id"]), participant_id, minutes=duration)
            raise LeaseConflict(
                "target is already leased", lease_id=str(existing["lease_id"]), region=target
            )
        expires_at = (now + timedelta(minutes=duration)).isoformat()
        lease_id = self._next_id("lease")
        self._conn.execute(
            """
            INSERT INTO leases(lease_id, participant_id, target, acquired_at, expires_at, released_at)
            VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (lease_id, participant_id, target, now.isoformat(), expires_at),
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

    @locked
    def renew(
        self,
        lease_id: str,
        participant_id: str,
        minutes: int | None = None,
        *,
        session: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if session is not None:
            self.require(session, "checkout")
            participant_id = str(session["participant_id"])
        duration = self.limits.default_lease_minutes if minutes is None else minutes
        row = self._conn.execute(
            "SELECT participant_id, target, acquired_at, expires_at, released_at FROM leases WHERE lease_id = ?",
            (lease_id,),
        ).fetchone()
        if row is None or row["released_at"] is not None:
            raise LeaseConflict("lease is not active", lease_id=lease_id)
        if str(row["participant_id"]) != participant_id:
            raise PermissionDenied("lease is held by another participant", lease_id=lease_id)
        if self._parse_time(str(row["expires_at"])) <= self._now():
            raise LeaseConflict("lease is not active", lease_id=lease_id)
        acquired = self._parse_time(str(row["acquired_at"]))
        max_expiry = acquired + timedelta(minutes=self.limits.max_lease_minutes)
        requested = self._now() + timedelta(minutes=duration)
        if requested > max_expiry:
            requested = max_expiry
        if requested <= self._now():
            raise LimitExceeded("lease cannot be renewed further", lease_id=lease_id)
        expires_at = requested.isoformat()
        self._conn.execute(
            "UPDATE leases SET expires_at = ? WHERE lease_id = ?", (expires_at, lease_id)
        )
        self._conn.commit()
        payload = {
            "lease_id": lease_id,
            "participant_id": participant_id,
            "target": str(row["target"]),
            "expires_at": expires_at,
        }
        self._record_event("lease.acquired", payload)
        return payload

    @locked
    def release(
        self,
        lease_id: str,
        participant_id: str | None = None,
        *,
        admin: bool = False,
    ) -> None:
        row = self._conn.execute(
            "SELECT participant_id, released_at FROM leases WHERE lease_id = ?",
            (lease_id,),
        ).fetchone()
        if row is None:
            raise LeaseConflict("lease not found", lease_id=lease_id)
        if participant_id and str(row["participant_id"]) != participant_id and not admin:
            raise PermissionDenied("lease is held by another participant", lease_id=lease_id)
        if row["released_at"] is not None:
            return
        self._conn.execute(
            "UPDATE leases SET released_at = ? WHERE lease_id = ? AND released_at IS NULL",
            (self._now_text(), lease_id),
        )
        self._conn.commit()
        self._record_event(
            "lease.released", {"lease_id": lease_id, "participant_id": str(row["participant_id"])}
        )

    @locked
    def proposals(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT proposal_id, workspace_id, participant_id, target, base_version, summary,
                   change_json, status, created_at
            FROM proposals ORDER BY created_at DESC
            """
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

    def _check_change_size(self, change: dict[str, Any]) -> None:
        encoded = json.dumps(change).encode("utf-8")
        if len(encoded) > self.limits.max_proposal_bytes:
            raise LimitExceeded("proposal is too large")

    @locked
    def propose(
        self,
        participant_id: str,
        target: str,
        base_version: int,
        summary: str,
        change: dict[str, Any],
        *,
        session: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if session is not None:
            self.require(session, "propose")
            participant_id = str(session["participant_id"])
        if not isinstance(change, dict) or not isinstance(change.get("content"), str):
            raise InvalidRequest("proposal change must contain string field 'content'")
        self._check_change_size(change)
        self._sync_substrate()
        self._validate_target(target)
        section = change.get("section")
        if isinstance(section, str) and section:
            self._validate_target(section)
        proposal_id = self._next_id("proposal")
        created_at = self._now_text()
        payload = {
            "proposal_id": proposal_id,
            "workspace_id": self.workspace_id,
            "participant_id": participant_id,
            "target": target,
            "base_version": base_version,
            "summary": summary,
            "change": change,
            "status": "pending",
            "created_at": created_at,
            "base_is_current": base_version == self.current_version,
        }
        self._conn.execute(
            """
            INSERT INTO proposals(
                proposal_id, workspace_id, participant_id, target, base_version,
                summary, change_json, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                self.workspace_id,
                participant_id,
                target,
                base_version,
                summary,
                json.dumps(change),
                "pending",
                created_at,
            ),
        )
        self._conn.commit()
        event_payload = {key: value for key, value in payload.items() if key != "change"}
        self._record_event("proposal.created", event_payload)
        return payload

    @locked
    def review_proposal(
        self,
        proposal_id: str,
        reviewer_id: str,
        approve: bool,
        *,
        session: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if session is not None:
            self.require(session, "approve")
            reviewer_id = str(session["participant_id"])
        row = self._conn.execute(
            "SELECT status FROM proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise ProposalNotFound("proposal not found", proposal_id=proposal_id)
        if str(row["status"]) != "pending":
            raise ProposalConflict("proposal is not pending", proposal_id=proposal_id)
        status = "approved" if approve else "rejected"
        self._conn.execute(
            "UPDATE proposals SET status = ?, review_by = ?, reviewed_at = ? WHERE proposal_id = ?",
            (status, reviewer_id, self._now_text(), proposal_id),
        )
        self._conn.commit()
        payload = {"proposal_id": proposal_id, "reviewer_id": reviewer_id, "status": status}
        self._record_event(f"proposal.{status}", payload)
        return payload

    def _conflict(self, base_version: int, attempted: str) -> StaleVersion:
        current_content = self._stored_content()
        diff = "".join(
            difflib.unified_diff(
                current_content.splitlines(keepends=True),
                attempted.splitlines(keepends=True),
                fromfile=f"v{self.current_version}",
                tofile=f"attempted@v{base_version}",
            )
        )
        changes = self.changes_since(base_version, limit=10)["changes"]
        return StaleVersion(
            "workspace version conflict",
            base_version=base_version,
            expected_version=base_version,
            current_version=self.current_version,
            diff=diff,
            changes=changes,
        )

    def _commit_content(
        self, participant_id: str | None, base_version: int, summary: str, content: str
    ) -> dict[str, Any]:
        self._sync_substrate()
        if base_version != self.current_version:
            raise self._conflict(base_version, content)
        payload = self._append_version(
            participant_id=participant_id,
            summary=summary,
            content=content,
            write_file=True,
        )
        self._supersede_pending(base_version)
        return payload

    def _supersede_pending(self, base_version: int) -> None:
        rows = self._conn.execute(
            "SELECT proposal_id FROM proposals WHERE status = 'pending' AND base_version <= ?",
            (base_version,),
        ).fetchall()
        if not rows:
            return
        self._conn.execute(
            "UPDATE proposals SET status = 'superseded' WHERE status = 'pending' AND base_version <= ?",
            (base_version,),
        )
        self._conn.commit()
        for row in rows:
            self._record_event("proposal.superseded", {"proposal_id": str(row["proposal_id"])})

    def _materialize_change(self, change: dict[str, Any]) -> str:
        content = change.get("content")
        if not isinstance(content, str):
            raise ProposalConflict("proposal change must contain string field 'content'")
        section = change.get("section")
        if isinstance(section, str) and section:
            return self._replace_section(self._stored_content(), section, content)
        return content

    @locked
    def commit(
        self,
        participant_id: str,
        base_version: int,
        summary: str,
        content: str,
        target: str = "workspace",
        *,
        session: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if session is not None:
            self.require(session, "admin")
            participant_id = str(session["participant_id"])
        payload = self._commit_content(participant_id, base_version, summary, content)
        payload["target"] = target
        return payload

    @locked
    def commit_proposal(
        self,
        proposal_id: str,
        participant_id: str,
        *,
        session: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if session is not None:
            self.require(session, "commit")
            participant_id = str(session["participant_id"])
        row = self._conn.execute(
            "SELECT base_version, summary, change_json, status FROM proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise ProposalNotFound("proposal not found", proposal_id=proposal_id)
        if str(row["status"]) != "approved":
            raise ProposalConflict(
                "proposal must be approved before commit", proposal_id=proposal_id
            )
        change = json.loads(str(row["change_json"]))
        try:
            content = self._materialize_change(change)
            payload = self._commit_content(
                participant_id, int(row["base_version"]), str(row["summary"]), content
            )
        except StaleVersion:
            self._conn.execute(
                "UPDATE proposals SET status = 'conflicted' WHERE proposal_id = ?",
                (proposal_id,),
            )
            self._conn.commit()
            self._record_event("proposal.conflicted", {"proposal_id": proposal_id})
            raise
        self._conn.execute(
            "UPDATE proposals SET status = 'committed' WHERE proposal_id = ?",
            (proposal_id,),
        )
        self._conn.commit()
        self._record_event(
            "proposal.committed", {"proposal_id": proposal_id, "version": payload["version"]}
        )
        return payload

    def _transport_for_invite(self) -> dict[str, Any]:
        current = self.current_transport()
        if current is not None:
            return current
        # No listener is attached. The hint stays opaque and names no vendor.
        return {"type": "local", "mode": "local", "encrypted": False}

    @locked
    def connection_diagnostics(self) -> dict[str, Any]:
        """Host-visible connection facts with endpoint secrets removed."""
        hint = dict(
            self.current_transport() or {"type": "local", "mode": "local", "encrypted": False}
        )
        for secret in ("address", "secret", "token", "base_url", "pid"):
            hint.pop(secret, None)
        return {
            "listener": hint,
            "participants": [
                {
                    "participant_id": person["participant_id"],
                    "name": person["name"],
                    "kind": person["kind"],
                    "transport": person.get("transport"),
                }
                for person in self.participants()
            ],
        }

    @locked
    def create_invite(
        self,
        permissions: str | None = None,
        target: str | None = None,
        expires_in_minutes: int = 60,
        max_uses: int | None = 1,
        capabilities: list[str] | None = None,
    ) -> dict[str, Any]:
        if capabilities is not None:
            granted = normalize_capabilities(capabilities)
        else:
            granted = capabilities_from_permissions(permissions)
        if expires_in_minutes > self.limits.max_invite_minutes:
            raise LimitExceeded(
                f"invitation duration exceeds max ({self.limits.max_invite_minutes} minutes)"
            )
        token = secrets.token_urlsafe(24)
        invite_id = self._next_id("invite")
        expires_at = (self._now() + timedelta(minutes=expires_in_minutes)).isoformat()
        self._conn.execute(
            """
            INSERT INTO invites(
                token, workspace_id, permissions, target, expires_at, max_uses,
                used_count, revoked_at, created_at, capabilities, invite_id
            ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?)
            """,
            (
                token,
                self.workspace_id,
                permissions or ",".join(granted),
                target,
                expires_at,
                max_uses,
                self._now_text(),
                json.dumps(granted),
                invite_id,
            ),
        )
        self._conn.commit()
        uri = encode_invitation(
            {
                "transport": self._transport_for_invite(),
                "workspace_id": self.workspace_id,
                "secret": token,
                "expires_at": expires_at,
                "capabilities": granted,
            }
        )
        self._record_event(
            "invite.created",
            {
                "invite_id": invite_id,
                "capabilities": granted,
                "expires_at": expires_at,
                "max_uses": max_uses,
            },
        )
        return {
            "invite_id": invite_id,
            "token": token,
            "uri": uri,
            "workspace_id": self.workspace_id,
            "permissions": permissions or ",".join(granted),
            "capabilities": granted,
            "target": target,
            "expires_at": expires_at,
            "max_uses": max_uses,
        }

    @locked
    def list_invites(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT invite_id, capabilities, permissions, target, expires_at, max_uses,
                   used_count, revoked_at, created_at
            FROM invites ORDER BY created_at DESC
            """
        ).fetchall()
        listed = []
        for row in rows:
            raw_caps = row["capabilities"]
            if raw_caps:
                caps = json.loads(str(raw_caps))
            else:
                caps = capabilities_from_permissions(str(row["permissions"]))
            listed.append(
                {
                    "invite_id": row["invite_id"],
                    "capabilities": caps,
                    "target": row["target"],
                    "expires_at": str(row["expires_at"]),
                    "max_uses": row["max_uses"],
                    "used_count": int(row["used_count"]),
                    "revoked_at": row["revoked_at"],
                    "created_at": str(row["created_at"]),
                }
            )
        return listed

    @locked
    def revoke_invite(self, token: str | None = None, invite_id: str | None = None) -> None:
        if invite_id:
            row = self._conn.execute(
                "SELECT token, invite_id, revoked_at FROM invites WHERE invite_id = ?",
                (invite_id,),
            ).fetchone()
        elif token:
            row = self._conn.execute(
                "SELECT token, invite_id, revoked_at FROM invites WHERE token = ?",
                (token,),
            ).fetchone()
        else:
            raise InvalidRequest("token or invite_id is required")
        if row is None:
            raise Unauthorized("invite not found")
        if row["revoked_at"] is None:
            self._conn.execute(
                "UPDATE invites SET revoked_at = ? WHERE token = ?",
                (self._now_text(), row["token"]),
            )
            self._conn.commit()
        self._record_event("invite.revoked", {"invite_id": row["invite_id"]})

    def _invite_capabilities(self, row: sqlite3.Row) -> list[str]:
        raw = row["capabilities"] if "capabilities" in row.keys() else None
        if raw:
            return normalize_capabilities(json.loads(str(raw)))
        return capabilities_from_permissions(str(row["permissions"]))

    def _consume_invite(self, token: str) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM invites WHERE token = ?", (token,)).fetchone()
        if row is None:
            raise Unauthorized("invite not found")
        if row["revoked_at"] is not None:
            raise InvitationRevoked()
        if self._parse_time(str(row["expires_at"])) < self._now():
            raise InvitationExpired()
        max_uses = row["max_uses"]
        if max_uses is not None and int(row["used_count"]) >= int(max_uses):
            raise LimitExceeded("invite usage limit reached")
        updated = self._conn.execute(
            """
            UPDATE invites SET used_count = used_count + 1
            WHERE token = ? AND revoked_at IS NULL
              AND (max_uses IS NULL OR used_count < max_uses)
            """,
            (token,),
        )
        self._conn.commit()
        if updated.rowcount != 1:
            raise LimitExceeded("invite usage limit reached")
        return {
            "token": token,
            "capabilities": self._invite_capabilities(row),
            "target": row["target"],
        }


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
