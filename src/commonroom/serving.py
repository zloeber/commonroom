from __future__ import annotations

import socket
import threading
import time
from typing import Any

import uvicorn

from commonroom.engine import WorkspaceEngine
from commonroom.errors import InvalidRequest, TransportUnavailable
from commonroom.http import create_app
from commonroom.transport.base import Transport
from commonroom.transport.registry import create_transport


class RunningRoom:
    def __init__(
        self,
        engine: WorkspaceEngine,
        *,
        local_url: str,
        invite: dict[str, Any],
        transport: Transport | None,
        servers: list[uvicorn.Server],
        threads: list[threading.Thread],
    ) -> None:
        self.engine = engine
        self.local_url = local_url
        self.invite = invite
        self.transport = transport
        self.servers = servers
        self.threads = threads

    def snapshot(self) -> dict[str, Any]:
        status = self.engine.status()
        return {
            "name": status["name"],
            "workspace_id": status["workspace_id"],
            "version": status["version"],
            "lifecycle": status["lifecycle"],
            "local_url": self.local_url,
            "invite": self.invite.get("uri"),
            "listener": self.engine.connection_diagnostics()["listener"],
            "participants": self.engine.participants(),
        }

    def banner(self) -> str:
        return render_banner(self.snapshot())

    def shutdown(self) -> None:
        try:
            self.engine.mark_stopped()
            self.engine.set_transport(None)
        except Exception:
            pass
        if self.transport is not None:
            self.transport.close()
        for server in self.servers:
            server.should_exit = True
        for thread in self.threads:
            thread.join(timeout=3)


def render_banner(snapshot: dict[str, Any]) -> str:
    lines = [
        "Commonroom",
        "",
        f"Workspace: {snapshot['name']}",
        f"Version: {snapshot['version']}",
        f"Status: {snapshot['lifecycle']}",
        "",
        "Human:",
        f"  {snapshot['local_url']}",
        "",
    ]
    listener = snapshot.get("listener") or {}
    if listener.get("type"):
        encrypted = "encrypted" if listener.get("encrypted") else "not encrypted"
        lines.extend(
            [
                "Transport:",
                f"  {listener['type']} ({listener.get('mode', 'local')}, {encrypted})",
            ]
        )
        if listener.get("mode") == "private" and listener.get("encrypted"):
            lines.append("  Status: READY")
        lines.append("")
    if snapshot.get("invite"):
        lines.extend(["Invite:", f"  {snapshot['invite']}", ""])
    lines.append("Participants:")
    people = snapshot.get("participants") or []
    if not people:
        lines.append("  (none)")
    for person in people:
        lines.append(f"  {person['kind']}: {person['name']}")
    return "\n".join(lines) + "\n"


def start_room(
    engine: WorkspaceEngine,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    name: str = "host",
    transport_name: str | None = None,
    transport: Transport | None = None,
) -> RunningRoom:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise InvalidRequest("refusing non-loopback bind; use a transport plugin for remote access")
    host_session = engine.ensure_host(name)
    owner = create_app(engine, access="owner", host_session=host_session)
    owner_server, owner_thread = _serve(owner, "127.0.0.1", port)
    plugin = transport
    servers = [owner_server]
    threads = [owner_thread]
    if transport_name and plugin is None:
        plugin = create_transport(transport_name)
    try:
        if plugin is not None:
            ingress_port = _pick_port()
            ingress = create_app(engine, access="session")
            ingress_server, ingress_thread = _serve(ingress, "127.0.0.1", ingress_port)
            servers.append(ingress_server)
            threads.append(ingress_thread)
            if not hasattr(plugin, "expose"):
                raise TransportUnavailable("transport plugin cannot expose a local port")
            engine.set_transport(plugin.expose(ingress_port))
        else:
            engine.set_transport(
                {
                    "type": "loopback",
                    "address": "127.0.0.1",
                    "port": port,
                    "encrypted": False,
                    "mode": "local",
                }
            )
        invite = engine.create_invite()
        engine.mark_started()
    except Exception:
        for server in servers:
            server.should_exit = True
        if plugin is not None:
            plugin.close()
        raise
    return RunningRoom(
        engine,
        local_url=f"http://127.0.0.1:{port}",
        invite=invite,
        transport=plugin,
        servers=servers,
        threads=threads,
    )


def _serve(app: Any, host: str, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    server.install_signal_handlers = False
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 8
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("workspace server did not start")
        time.sleep(0.02)
    return server, thread


def _pick_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
