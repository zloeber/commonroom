from __future__ import annotations

from typing import Any, Protocol

# Advertised by plugins. The workspace engine does not branch on these.
CAPABILITY_NAMES = frozenset(
    {
        "direct_peer_connection",
        "relay_support",
        "streaming",
        "bidirectional",
        "browser_compatible",
        "large_file_transfer",
        "multiplexing",
        "offline_resume",
        "encrypted",
    }
)


class Connection(Protocol):
    """One bidirectional byte stream over a transport plugin."""

    def send(self, data: bytes) -> None: ...

    def receive(self, max_bytes: int = 65536) -> bytes: ...

    def close(self) -> None: ...


class Transport(Protocol):
    """Replaceable connectivity underneath the Commonroom protocol.

    Plugins live behind ``commonroom.transports`` entry points. The workspace
    engine stores an opaque endpoint hint and never imports a plugin.
    """

    def listen(self) -> dict[str, Any]: ...

    def connect(self, description: dict[str, Any]) -> Connection: ...

    def accept(self) -> Connection: ...

    def close(self) -> None: ...

    def describe(self) -> dict[str, Any]: ...

    def capabilities(self) -> frozenset[str]: ...
