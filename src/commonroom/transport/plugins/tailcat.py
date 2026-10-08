from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import threading
import time
from typing import Any

from commonroom.errors import TransportUnavailable
from commonroom.transport.connection import SocketConnection

# Endpoint material is a transport secret. Match only the address token.
_ADDRESS = re.compile(r"\b(tc[A-Za-z0-9_-]{16,})\b")
_INSTALL = "https://github.com/tailscale/tailcat#install"


def parse_endpoint(text: str) -> str | None:
    match = _ADDRESS.search(text)
    if match is None:
        return None
    return match.group(1)


def redact(text: str) -> str:
    return _ADDRESS.sub("tc…", text)


class TailcatTransport:
    """First private-rendezvous plugin.

    The binary provides the encrypted channel. This module does not implement
    that cryptography, and nothing above the transport API imports it.
    """

    name = "tailcat"

    def __init__(self, binary: str | None = None, timeout_seconds: float = 20.0) -> None:
        self.binary = binary or os.environ.get("COMMONROOM_TAILCAT") or "tailcat"
        self.timeout_seconds = timeout_seconds
        self.address: str | None = None
        self.port: int | None = None
        self.local_url: str | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._server: socket.socket | None = None
        self._drain: threading.Thread | None = None

    def capabilities(self) -> frozenset[str]:
        return frozenset(
            {
                "direct_peer_connection",
                "relay_support",
                "streaming",
                "bidirectional",
                "encrypted",
                "large_file_transfer",
            }
        )

    def serve_command(self, port: int) -> list[str]:
        # A fresh key keeps a restarted room from reviving a shared endpoint.
        return [self.binary, "serve", "--key=new", str(port)]

    def forward_command(self, address: str, local_port: int, remote_port: int) -> list[str]:
        return [self.binary, "forward", address, f"{local_port}:{remote_port}"]

    def expose(self, port: int) -> dict[str, Any]:
        """Forward an already-listening local TCP port, usually the HTTP ingress."""
        self._start_serve(port)
        return self.describe()

    def listen(self) -> dict[str, Any]:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(16)
        server.settimeout(self.timeout_seconds)
        self._server = server
        port = int(server.getsockname()[1])
        self._start_serve(port)
        return self.describe()

    def accept(self) -> SocketConnection:
        if self._server is None:
            raise TransportUnavailable("listen before accept")
        try:
            sock, _ = self._server.accept()
        except TimeoutError as exc:
            raise TransportUnavailable("accept timed out") from exc
        except OSError as exc:
            raise TransportUnavailable("accept failed") from exc
        return SocketConnection(sock, timeout=self.timeout_seconds)

    def connect(self, description: dict[str, Any]) -> SocketConnection:
        address = str(description.get("address") or "")
        remote_port = description.get("port")
        if not address.startswith("tc") or not isinstance(remote_port, int):
            raise TransportUnavailable("transport description is missing an endpoint")
        local_port = _ephemeral_port()
        self._require_binary()
        self.address = address
        self.port = remote_port
        self._proc = self._popen(self.forward_command(address, local_port, remote_port))
        self._start_drain()
        if not _wait_until_accepts(local_port, self.timeout_seconds):
            output = self._stop_and_collect()
            raise TransportUnavailable(
                "transport forward did not open a local port", output=redact(output)[-1000:]
            )
        self.local_url = f"http://127.0.0.1:{local_port}"
        try:
            sock = socket.create_connection(("127.0.0.1", local_port), timeout=self.timeout_seconds)
        except OSError as exc:
            raise TransportUnavailable("connect failed") from exc
        return SocketConnection(sock, timeout=self.timeout_seconds)

    def close(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
        proc = self._proc
        self._proc = None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)

    def describe(self) -> dict[str, Any]:
        if not self.address or self.port is None:
            raise TransportUnavailable("transport has no endpoint yet")
        return {
            "type": self.name,
            "address": self.address,
            "port": self.port,
            "encrypted": True,
            "mode": "private",
        }

    def _start_serve(self, port: int) -> None:
        self._require_binary()
        self.port = port
        self._proc = self._popen(self.serve_command(port))
        self.address = self._read_endpoint()
        self._start_drain()

    def _require_binary(self) -> None:
        if os.path.isabs(self.binary) and os.path.exists(self.binary):
            return
        if shutil.which(self.binary) is None:
            raise TransportUnavailable(
                f"transport plugin '{self.name}' is not available",
                install=_INSTALL,
            )

    def _popen(self, command: list[str]) -> subprocess.Popen[str]:
        return subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def _read_endpoint(self) -> str:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise TransportUnavailable("transport process did not start")
        deadline = time.monotonic() + self.timeout_seconds
        collected: list[str] = []
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                collected.append(proc.stdout.read() or "")
                break
            line = proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            collected.append(line)
            endpoint = parse_endpoint(line)
            if endpoint:
                return endpoint
        raise TransportUnavailable(
            "transport did not publish an endpoint",
            install=_INSTALL,
            output=redact("".join(collected))[-1000:],
        )

    def _start_drain(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return

        def _drain() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                redact(line)

        self._drain = threading.Thread(target=_drain, name="transport-output", daemon=True)
        self._drain.start()

    def _stop_and_collect(self) -> str:
        proc = self._proc
        if proc is None:
            return ""
        if proc.poll() is None:
            proc.terminate()
        try:
            output, _ = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            output, _ = proc.communicate(timeout=3)
        self._proc = None
        return output or ""


def plugin(**kwargs: Any) -> TailcatTransport:
    return TailcatTransport(**kwargs)


def _ephemeral_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_accepts(port: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.2)
            try:
                probe.connect(("127.0.0.1", port))
                return True
            except OSError:
                time.sleep(0.1)
    return False
