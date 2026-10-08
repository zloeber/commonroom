from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

from commonroom.errors import TransportUnavailable
from commonroom.transport.connection import read_exact
from commonroom.transport.loopback import LoopbackTransport
from commonroom.transport.registry import create_transport, register_transport


def _frame(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + payload


def _serve_frames(transport: LoopbackTransport, count: int, seen: list[bytes]) -> None:
    for _ in range(count):
        connection = transport.accept()
        try:
            size = int.from_bytes(read_exact(connection, 4), "big")
            body = read_exact(connection, size)
            seen.append(body)
            connection.send(b"ack")
        finally:
            connection.close()


def test_loopback_conformance_roundtrip() -> None:
    transport = LoopbackTransport()
    try:
        endpoint = transport.listen()
        seen: list[bytes] = []
        server = threading.Thread(target=_serve_frames, args=(transport, 1, seen))
        server.start()
        client = create_transport(endpoint["type"]).connect(endpoint)
        payload = b"hello commonroom"
        client.send(_frame(payload))
        assert read_exact(client, 3) == b"ack"
        client.close()
        server.join(timeout=2)
        assert seen == [payload]
    finally:
        transport.close()


def test_loopback_large_payload_and_concurrent_streams() -> None:
    transport = LoopbackTransport()
    try:
        endpoint = transport.listen()
        seen: list[bytes] = []
        server = threading.Thread(target=_serve_frames, args=(transport, 2, seen))
        server.start()
        payloads = [b"a" * 200_000, b"b" * 180_000]

        def _send(payload: bytes) -> None:
            connection = create_transport("loopback").connect(endpoint)
            connection.send(_frame(payload))
            assert read_exact(connection, 3) == b"ack"
            connection.close()

        threads = [threading.Thread(target=_send, args=(payload,)) for payload in payloads]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        server.join(timeout=5)
        assert sorted(seen) == sorted(payloads)
    finally:
        transport.close()


def test_loopback_reconnect_timeout_and_failure() -> None:
    transport = LoopbackTransport(timeout=0.3)
    try:
        endpoint = transport.listen()
        with pytest.raises(TransportUnavailable, match="timed out"):
            transport.accept()
        seen: list[bytes] = []
        server = threading.Thread(target=_serve_frames, args=(transport, 2, seen))
        server.start()
        for payload in (b"one", b"two"):
            connection = create_transport("loopback").connect(endpoint)
            connection.send(_frame(payload))
            assert read_exact(connection, 3) == b"ack"
            connection.close()
            with pytest.raises(TransportUnavailable):
                connection.send(b"again")
        server.join(timeout=2)
        with pytest.raises(TransportUnavailable):
            create_transport("loopback").connect(
                {"type": "loopback", "address": "127.0.0.1", "port": 1}
            )
    finally:
        transport.close()


def test_custom_plugin_registers_without_touching_the_engine() -> None:
    class Marker:
        name = "marker"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def capabilities(self):
            return frozenset({"streaming"})

        def listen(self):
            return {"type": "marker"}

        def connect(self, description):
            self.calls.append("connect")
            raise TransportUnavailable("marker does not dial in this test")

        def accept(self):
            raise TransportUnavailable("marker has no listener")

        def close(self) -> None:
            self.calls.append("close")

        def describe(self):
            return {"type": "marker"}

    register_transport("marker", Marker)
    plugin = create_transport("marker")
    assert plugin.describe()["type"] == "marker"
    with pytest.raises(TransportUnavailable):
        plugin.connect({"type": "marker"})


def test_tailcat_plugin_is_loaded_only_when_requested() -> None:
    import sys

    sys.modules.pop("commonroom.transport.plugins.tailcat", None)
    import commonroom.engine  # noqa: F401

    assert "commonroom.transport.plugins.tailcat" not in sys.modules
    try:
        create_transport("tailcat")
    except TransportUnavailable:
        pass
    assert "commonroom.transport.plugins.tailcat" in sys.modules


def test_core_modules_do_not_name_transport_vendors() -> None:
    root = Path("src/commonroom")
    for relative in ("engine.py", "http.py", "protocol.py", "gui.py", "serving.py"):
        source = (root / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        modules = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert all("tailcat" not in module for module in modules)
        lowered = source.lower()
        for vendor in ("tailcat", "libp2p", "iroh", "webrtc"):
            assert vendor not in lowered
