from __future__ import annotations

import socket

from commonroom.errors import TransportUnavailable


class SocketConnection:
    """Byte stream over one TCP socket. Plugins share this; they do not share policy."""

    def __init__(self, sock: socket.socket, *, timeout: float = 10.0) -> None:
        self._sock = sock
        self._timeout = timeout
        self._closed = False
        self._sock.settimeout(timeout)

    def send(self, data: bytes) -> None:
        if self._closed:
            raise TransportUnavailable("connection is closed")
        view = memoryview(data)
        try:
            while view:
                sent = self._sock.send(view)
                if sent == 0:
                    raise TransportUnavailable("connection is closed")
                view = view[sent:]
        except OSError as exc:
            raise TransportUnavailable("send failed") from exc

    def receive(self, max_bytes: int = 65536) -> bytes:
        if self._closed:
            raise TransportUnavailable("connection is closed")
        try:
            return self._sock.recv(max_bytes)
        except TimeoutError as exc:
            raise TransportUnavailable("receive timed out") from exc
        except OSError as exc:
            raise TransportUnavailable("receive failed") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass


def read_exact(connection: SocketConnection, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = connection.receive(min(remaining, 65536))
        if not chunk:
            raise TransportUnavailable("connection closed before the payload finished")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
