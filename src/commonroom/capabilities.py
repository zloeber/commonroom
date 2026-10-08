from __future__ import annotations

# Composable capability names. ``*`` is the host and implies every capability.
# ``admin`` is the same implication without using the wildcard spelling.
CAPABILITIES = (
    "read",
    "observe",
    "propose",
    "checkout",
    "commit",
    "approve",
    "invite",
    "admin",
)

# A remote agent can collaborate, but cannot approve, invite, or administer.
DEFAULT_AGENT_CAPABILITIES = ["read", "observe", "propose", "checkout", "commit"]

HOST_CAPABILITIES = ["*"]

_LEGACY_PERMISSIONS = {
    "read_write": DEFAULT_AGENT_CAPABILITIES,
    "read": ["read", "observe"],
    "admin": ["*"],
    "*": ["*"],
}


def normalize_capabilities(values: list[str] | None) -> list[str]:
    if not values:
        return []
    cleaned: list[str] = []
    for value in values:
        item = value.strip()
        if not item:
            continue
        if item not in CAPABILITIES and item != "*":
            raise ValueError(f"unknown capability '{item}'")
        if item not in cleaned:
            cleaned.append(item)
    if "*" in cleaned:
        return ["*"]
    return cleaned


def capabilities_from_permissions(permissions: str | None) -> list[str]:
    if permissions is None:
        return list(DEFAULT_AGENT_CAPABILITIES)
    mapped = _LEGACY_PERMISSIONS.get(permissions)
    if mapped is not None:
        return list(mapped)
    return normalize_capabilities(permissions.split(","))


def has_capability(granted: list[str], required: str) -> bool:
    if "*" in granted or "admin" in granted:
        return True
    return required in granted
