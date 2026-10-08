from __future__ import annotations

from typing import Any

# Fields agents can read without digging through prose.
_RECOVERY_FIELDS = {
    "base_version",
    "current_version",
    "expected_version",
    "diff",
    "changes",
    "retry_after_seconds",
    "lease_id",
    "proposal_id",
    "region",
}


class CommonroomError(ValueError):
    """Machine-actionable workspace error.

    Adapters should serialize ``to_dict()`` and use ``http_status``. The
    message is for humans; clients must branch on ``code``.
    """

    code = "error"
    http_status = 400

    def __init__(self, message: str, **details: Any) -> None:
        self.message = message
        self.details = details
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": self.code, "message": self.message}
        for key, value in self.details.items():
            if key in _RECOVERY_FIELDS:
                body[key] = value
        if self.details:
            body["details"] = self.details
        return body

    @property
    def payload(self) -> dict[str, Any]:
        return self.to_dict()


class Unauthorized(CommonroomError):
    code = "unauthorized"
    http_status = 401


class PermissionDenied(CommonroomError):
    code = "permission_denied"
    http_status = 403


class InvitationExpired(CommonroomError):
    code = "invitation_expired"
    http_status = 403

    def __init__(self, message: str = "invite expired", **details: Any) -> None:
        super().__init__(message, **details)


class InvitationRevoked(CommonroomError):
    code = "invitation_revoked"
    http_status = 403

    def __init__(self, message: str = "invite revoked", **details: Any) -> None:
        super().__init__(message, **details)


class SessionExpired(CommonroomError):
    code = "session_expired"
    http_status = 401

    def __init__(self, message: str = "session expired", **details: Any) -> None:
        super().__init__(message, **details)


class LeaseConflict(CommonroomError):
    code = "lease_conflict"
    http_status = 409


class StaleVersion(CommonroomError):
    code = "stale_version"
    http_status = 409


WorkspaceConflictError = StaleVersion


class ProposalNotFound(CommonroomError):
    code = "proposal_not_found"
    http_status = 404


class ProposalConflict(CommonroomError):
    code = "proposal_conflict"
    http_status = 409


class InvalidRegion(CommonroomError):
    code = "invalid_region"
    http_status = 400


class VersionNotFound(CommonroomError):
    code = "version_not_found"
    http_status = 404


class TransportUnavailable(CommonroomError):
    code = "transport_unavailable"
    http_status = 503


class LimitExceeded(CommonroomError):
    code = "limit_exceeded"
    http_status = 429


class InvalidRequest(CommonroomError):
    code = "invalid_request"
    http_status = 400
