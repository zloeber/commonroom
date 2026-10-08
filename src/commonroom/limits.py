from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    """Conservative bounds for a workspace exposed to remote agents."""

    max_request_bytes: int = 1_048_576
    max_proposal_bytes: int = 262_144
    max_active_sessions: int = 32
    max_events: int = 2_000
    max_lease_minutes: int = 120
    default_lease_minutes: int = 15
    default_invite_minutes: int = 60
    max_invite_minutes: int = 60 * 24 * 7
    default_session_minutes: int = 8 * 60
    max_session_minutes: int = 24 * 60
    max_requests_per_minute: int = 600
    max_list_items: int = 200
