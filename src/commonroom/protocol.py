from __future__ import annotations

import base64
import json
from typing import Any

PROTOCOL_NAME = "commonroom"
PROTOCOL_VERSION = "1"
INVITATION_PREFIX = "commonroom://join/"
MAX_INVITATION_BYTES = 8_192

# Semantic operations. HTTP paths are one adapter, not the protocol.
OPERATIONS: list[dict[str, str]] = [
    {"name": "workspace.status", "http": "GET /workspace/status", "capability": "read"},
    {"name": "workspace.summary", "http": "GET /workspace/summary", "capability": "read"},
    {"name": "workspace.regions", "http": "GET /workspace/regions", "capability": "read"},
    {"name": "workspace.read", "http": "GET /workspace/read", "capability": "read"},
    {"name": "workspace.diff", "http": "GET /workspace/diff", "capability": "read"},
    {
        "name": "workspace.changes_since",
        "http": "GET /workspace/changes_since",
        "capability": "read",
    },
    {"name": "workspace.observe", "http": "GET /workspace/observe", "capability": "observe"},
    {"name": "workspace.checkout", "http": "POST /workspace/checkout", "capability": "checkout"},
    {"name": "workspace.release", "http": "POST /workspace/release", "capability": "checkout"},
    {"name": "workspace.renew", "http": "POST /workspace/renew", "capability": "checkout"},
    {"name": "workspace.propose", "http": "POST /workspace/propose", "capability": "propose"},
    {
        "name": "workspace.review",
        "http": "POST /workspace/proposal/review",
        "capability": "approve",
    },
    {"name": "workspace.commit", "http": "POST /workspace/commit", "capability": "commit"},
    {"name": "workspace.participants", "http": "GET /workspace/participants", "capability": "read"},
    {"name": "workspace.leases", "http": "GET /workspace/leases", "capability": "read"},
    {"name": "workspace.proposals", "http": "GET /workspace/proposals", "capability": "read"},
    {"name": "workspace.history", "http": "GET /workspace/history", "capability": "read"},
    {"name": "workspace.diagnostics", "http": "GET /workspace/diagnostics", "capability": "read"},
    {"name": "workspace.invite", "http": "POST /workspace/invite", "capability": "invite"},
    {"name": "workspace.leave", "http": "POST /workspace/leave", "capability": "read"},
    {"name": "session.join", "http": "POST /session/join", "capability": "invitation"},
]

ERROR_CODES = [
    "unauthorized",
    "permission_denied",
    "invitation_expired",
    "invitation_revoked",
    "invitation_redeemed",
    "unsupported_version",
    "session_expired",
    "lease_conflict",
    "stale_version",
    "proposal_not_found",
    "proposal_conflict",
    "invalid_region",
    "version_not_found",
    "transport_unavailable",
    "limit_exceeded",
    "invalid_request",
]

WORKFLOW = [
    "session.join",
    "workspace.status",
    "workspace.summary",
    "workspace.read",
    "workspace.checkout",
    "workspace.propose",
    "workspace.review",
    "workspace.commit",
    "workspace.release",
]


def discovery_document(workspace_id: str) -> dict[str, Any]:
    return {
        "name": "Commonroom",
        "protocol": PROTOCOL_NAME,
        "protocol_version": PROTOCOL_VERSION,
        "workspace_id": workspace_id,
        "discovery": {
            "protocol": "/protocol",
            "skills": "/skills",
            "skill": "/skills/commonroom",
            "join": "/session/join",
        },
        "operations": [item["name"] for item in OPERATIONS],
        "workflow": WORKFLOW,
        "errors": ERROR_CODES,
    }


def protocol_document() -> dict[str, Any]:
    return {
        "protocol": PROTOCOL_NAME,
        "version": PROTOCOL_VERSION,
        "operations": OPERATIONS,
        "errors": ERROR_CODES,
        "workflow": WORKFLOW,
        "auth": {
            "header": "Authorization: Bearer <session_id>",
            "join": "POST /session/join",
            "note": "A transport endpoint does not authorize workspace operations. Join with an invitation to receive a session.",
        },
        "bounds": ["max_bytes", "max_lines", "max_items", "max_depth"],
        "transports": {
            "entry_point_group": "commonroom.transports",
            "methods": ["listen", "connect", "accept", "send", "receive", "close"],
            "note": "Invitations carry opaque transport hints. The workspace engine does not import transport plugins.",
        },
        "artifacts": {
            "note": "Artifact bytes move through the active transport. There is no second network stack.",
            "operations": ["workspace.read", "workspace.commit"],
        },
    }


def encode_invitation(document: dict[str, Any]) -> str:
    hints = document.get("transports") or [document["transport"]]
    payload = {
        "protocol": PROTOCOL_NAME,
        "version": PROTOCOL_VERSION,
        "transport": hints[0],
        "transports": hints,
        "workspace_id": document["workspace_id"],
        "secret": document["secret"],
        "expires_at": document["expires_at"],
        "capabilities": document["capabilities"],
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(raw) > MAX_INVITATION_BYTES:
        raise ValueError("invitation too large")
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{INVITATION_PREFIX}{token}"


def decode_invitation(value: str) -> dict[str, Any]:
    text = value.strip()
    if text.startswith("{"):
        document = json.loads(text)
    else:
        if text.startswith(INVITATION_PREFIX):
            text = text[len(INVITATION_PREFIX) :]
        padding = "=" * (-len(text) % 4)
        try:
            raw = base64.urlsafe_b64decode(text + padding)
        except Exception as exc:
            raise ValueError("invitation is not a commonroom capability") from exc
        if len(raw) > MAX_INVITATION_BYTES:
            raise ValueError("invitation too large")
        document = json.loads(raw.decode("utf-8"))
    if document.get("protocol") != PROTOCOL_NAME:
        raise ValueError("invitation protocol is not commonroom")
    if str(document.get("version")) != PROTOCOL_VERSION:
        raise ValueError("unsupported invitation version")
    for key in ("transport", "workspace_id", "secret", "expires_at", "capabilities"):
        if key not in document:
            raise ValueError(f"invitation missing {key}")
    return document
