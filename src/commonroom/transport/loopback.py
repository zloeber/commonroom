from __future__ import annotations

import socket
from typing import Any

from commonroom.errors import TransportUnavailable
from commonroom.transport.connection import SocketConnection


class LoopbackTransport:
    """Local TCP transport for development and the conformance suite.

    This is the reference plugin. Other plugins must pass the same tests.
    """

    name = "loopback"

    def __init__(self, *, timeout: float = 5.0) -> None:
        self.timeout = timeout
        self._server: socket.socket | None = None
        self._description: dict[str, Any] | None = None

    def capabilities(self) -> frozenset[str]:
        return frozenset({"streaming", "bidirectional", "large_file_transfer", "multiplexing"})

    def listen(self) -> dict[str, Any]:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(16)
        server.settimeout(self.timeout)
        self._server = server
        host, port = server.getsockname()
        self._description = {
            "type": self.name,
            "address": host,
            "port": port,
            "encrypted": False,
            "mode": "local",
        }
        return dict(self._description)

    def expose(self, port: int, host: str = "127.0.0.1") -> dict[str, Any]:
        """Describe an existing local listener without inserting another stack."""
        self._description = {
            "type": self.name,
            "address": host,
            "port": port,
            "encrypted": False,
            "mode": "local",
        }
        return dict(self._description)

    def accept(self) -> SocketConnection:
        if self._server is None:
            raise TransportUnavailable("listen before accept")
        try:
            sock, _ = self._server.accept()
        except TimeoutError as exc:
            raise TransportUnavailable("accept timed out") from exc
        except OSError as exc:
            raise TransportUnavailable("accept failed") from exc
        return SocketConnection(sock, timeout=self.timeout)

    def connect(self, description: dict[str, Any]) -> SocketConnection:
        host = str(description.get("address") or "127.0.0.1")
        port = description.get("port")
        if not isinstance(port, int):
            raise TransportUnavailable("loopback description is missing a port")
        try:
            sock = socket.create_connection((host, port), timeout=self.timeout)
        except OSError as exc:
            raise TransportUnavailable("connect failed") from exc
        return SocketConnection(sock, timeout=self.timeout)

    def close(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()

    def describe(self) -> dict[str, Any]:
        if self._description is None:
            raise TransportUnavailable("transport is not listening")
        return dict(self._description)


def plugin(**kwargs: Any) -> LoopbackTransport:
    return LoopbackTransport(**kwargs)
