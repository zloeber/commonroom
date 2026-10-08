from __future__ import annotations

from collections.abc import Callable
from typing import Any

from commonroom.errors import TransportUnavailable
from commonroom.transport.base import Connection, Transport

Factory = Callable[..., Transport]

_FACTORIES: dict[str, Factory] = {}


def register_transport(name: str, factory: Factory) -> None:
    """Register a transport plugin. Entry points call this when they load."""
    _FACTORIES[name] = factory


def registered_transports() -> list[str]:
    _load_builtin()
    return sorted(_FACTORIES)


def create_transport(name: str, **kwargs: Any) -> Transport:
    _load_builtin()
    factory = _FACTORIES.get(name)
    if factory is None:
        _load_entrypoint(name)
        factory = _FACTORIES.get(name)
    if factory is None:
        raise TransportUnavailable(
            f"no transport plugin named '{name}' is installed",
            transport=name,
            entry_point_group="commonroom.transports",
        )
    return factory(**kwargs)


def invitation_hints(document: dict[str, Any]) -> list[dict[str, Any]]:
    hints = document.get("transports")
    if isinstance(hints, list) and hints:
        return [hint for hint in hints if isinstance(hint, dict)]
    hint = document.get("transport")
    if isinstance(hint, dict):
        return [hint]
    return []


def connect_invitation(document: dict[str, Any]) -> tuple[Transport, Connection, dict[str, Any]]:
    """Dial the first advertised hint whose plugin is installed.

    Negotiation stops at plugin availability. It does not grant a session.
    """
    errors: list[str] = []
    for hint in invitation_hints(document):
        name = str(hint.get("type") or "")
        try:
            plugin = create_transport(name)
        except TransportUnavailable as exc:
            errors.append(exc.message)
            continue
        try:
            return plugin, plugin.connect(hint), hint
        except Exception:
            plugin.close()
            raise
    raise TransportUnavailable(
        "no advertised transport plugin is available",
        tried=errors,
    )


def _load_builtin() -> None:
    if "loopback" in _FACTORIES:
        return
    from commonroom.transport.loopback import plugin

    register_transport("loopback", plugin)


def _load_entrypoint(name: str) -> None:
    from importlib.metadata import entry_points

    group = entry_points(group="commonroom.transports")
    for item in group:
        if item.name != name:
            continue
        loaded = item.load()
        register_transport(name, loaded)
        return
