from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from commonroom.client import http_exchange
from commonroom.engine import WorkspaceEngine
from commonroom.errors import CommonroomError, InvalidRequest, UnsupportedVersion
from commonroom.protocol import decode_invitation
from commonroom.serving import start_room
from commonroom.transport.registry import connect_invitation

COMMANDS = {
    "serve",
    "status",
    "invite",
    "join",
    "call",
    "participants",
    "leases",
    "proposals",
    "history",
    "diff",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="commonroom")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Open a room around a local substrate")
    serve.add_argument("path", help="Path to the workspace substrate")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--name", default=os.environ.get("USER") or "host")
    serve.add_argument(
        "--tailcat", action="store_true", help="Expose the room through the Tailcat plugin"
    )
    serve.add_argument("--transport", default=None, help="Transport plugin name")
    serve.add_argument(
        "--ephemeral", action="store_true", help="Keep metadata only for this process"
    )
    serve.add_argument("--json", action="store_true", dest="as_json")

    status = subparsers.add_parser("status", help="Show workspace status")
    status.add_argument(
        "--workspace", default=os.environ.get("COMMONROOM_WORKSPACE", "workspace.md")
    )
    status.add_argument("--json", action="store_true", dest="as_json")

    invite = subparsers.add_parser("invite", help="Create an invitation")
    invite.add_argument(
        "--workspace", default=os.environ.get("COMMONROOM_WORKSPACE", "workspace.md")
    )
    invite.add_argument("--permissions", default=None)
    invite.add_argument("--capabilities", default="read,observe,propose,checkout,commit")
    invite.add_argument("--target")
    invite.add_argument("--expires", type=int, default=60)
    invite.add_argument("--max-uses", type=int, default=1)
    invite.add_argument("--json", action="store_true", dest="as_json")

    join = subparsers.add_parser("join", help="Join with an invitation")
    join.add_argument("invite")
    join.add_argument("--workspace", default=os.environ.get("COMMONROOM_WORKSPACE", "workspace.md"))
    join.add_argument("--kind", default="agent", choices=["human", "agent"])
    join.add_argument("--name", default="participant")
    join.add_argument("--hold", action="store_true")
    join.add_argument("--no-hold", action="store_true")
    join.add_argument("--json", action="store_true", dest="as_json")

    call = subparsers.add_parser("call", help="Send one request through an invitation")
    call.add_argument("invite")
    call.add_argument("method", choices=["GET", "POST", "get", "post"])
    call.add_argument("path")
    call.add_argument("--session", default=None)
    call.add_argument("--body", default=None, help="JSON request body")

    for name, help_text in (
        ("participants", "List participants"),
        ("leases", "List active leases"),
        ("proposals", "List proposals"),
        ("history", "Show recent events"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument(
            "--workspace", default=os.environ.get("COMMONROOM_WORKSPACE", "workspace.md")
        )
        command.add_argument("--json", action="store_true", dest="as_json")

    diff = subparsers.add_parser("diff", help="Show the diff since a version")
    diff.add_argument("--workspace", default=os.environ.get("COMMONROOM_WORKSPACE", "workspace.md"))
    diff.add_argument("--base", type=int, required=True)
    diff.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        argv = ["serve", *argv]
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "serve":
            return _serve(args)
        return _local(args)
    except CommonroomError as exc:
        print(json.dumps(exc.to_dict(), indent=2))
        return 1


def _serve(args: argparse.Namespace) -> int:
    db_path = None
    ephemeral_dir = None
    if args.ephemeral:
        ephemeral_dir = tempfile.mkdtemp(prefix="commonroom-")
        db_path = str(Path(ephemeral_dir) / "workspace.db")
    engine = WorkspaceEngine(args.path, db_path=db_path)
    transport_name = args.transport or ("tailcat" if args.tailcat else None)
    try:
        room = start_room(
            engine,
            host=args.host,
            port=args.port,
            name=args.name,
            transport_name=transport_name,
        )
        try:
            if args.as_json:
                print(json.dumps(room.snapshot(), indent=2), flush=True)
            else:
                print(room.banner(), end="", flush=True)
            while any(thread.is_alive() for thread in room.threads):
                time.sleep(0.3)
        except KeyboardInterrupt:
            pass
        finally:
            room.shutdown()
    finally:
        engine.close()
        if ephemeral_dir is not None:
            shutil.rmtree(ephemeral_dir, ignore_errors=True)
    return 0


def _local(args: argparse.Namespace) -> int:
    if args.command == "join" and _is_remote(args.invite):
        return _join_remote(args)
    if args.command == "call":
        return _call_remote(args)
    engine = WorkspaceEngine(args.workspace)
    try:
        if args.command == "status":
            payload = engine.status()
            payload["listener"] = engine.connection_diagnostics()["listener"]
            return _emit(payload, args.as_json, _status_text)
        if args.command == "invite":
            capabilities = [item.strip() for item in args.capabilities.split(",") if item.strip()]
            payload = engine.create_invite(
                permissions=args.permissions,
                capabilities=capabilities,
                target=args.target,
                expires_in_minutes=args.expires,
                max_uses=args.max_uses,
            )
            return _emit(payload, args.as_json, lambda item: item["uri"] + "\n")
        if args.command == "join":
            payload = engine.join(kind=args.kind, name=args.name, invite_token=args.invite)
            return _emit(payload, args.as_json, lambda item: item["participant_id"] + "\n")
        if args.command == "participants":
            return _emit(engine.participants(), args.as_json, _people_text)
        if args.command == "leases":
            return _emit(engine.leases(), args.as_json, _leases_text)
        if args.command == "proposals":
            return _emit(engine.proposals(), args.as_json, _proposals_text)
        if args.command == "history":
            return _emit(engine.history(), args.as_json, _history_text)
        if args.command == "diff":
            payload = engine.diff(args.base)
            return _emit(payload, args.as_json, lambda item: item["diff"])
    finally:
        engine.close()
    return 1


def _join_remote(args: argparse.Namespace) -> int:
    document = _invitation_document(args.invite)
    plugin, connection, hint = connect_invitation(document)
    try:
        status, body = http_exchange(
            connection,
            "POST",
            "/session/join",
            json_body={
                "invite": document["secret"],
                "name": args.name,
                "kind": args.kind,
                "transport": hint.get("type", "remote"),
            },
        )
    finally:
        connection.close()
    if status != 200:
        print(json.dumps(body, indent=2))
        plugin.close()
        return 1
    if body.get("workspace_id") != document["workspace_id"]:
        session_id = body.get("session_id")
        plugin.close()
        if session_id:
            _remote(document, "POST", "/workspace/leave", session=str(session_id))
        print(
            json.dumps(
                {
                    "error": "invalid_request",
                    "message": "session workspace does not match the invitation",
                },
                indent=2,
            )
        )
        return 1
    if args.as_json:
        print(json.dumps(body, indent=2))
    else:
        print(body.get("participant_id", ""))
    hold = args.hold or (hint.get("type") not in {"loopback", "local"} and not args.no_hold)
    if hold and getattr(plugin, "local_url", None):
        print(f"Local: {plugin.local_url}")
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    plugin.close()
    return 0


def _call_remote(args: argparse.Namespace) -> int:
    if not _is_remote(args.invite):
        raise InvalidRequest("call requires a commonroom:// invitation")
    document = _invitation_document(args.invite)
    method = args.method.upper()
    path = str(args.path)
    if not path.startswith("/") or path.startswith("//"):
        raise InvalidRequest("path must be a Commonroom HTTP path")
    body = None
    if args.body is not None:
        try:
            body = json.loads(args.body)
        except json.JSONDecodeError as exc:
            raise InvalidRequest("body must be JSON") from exc
        if not isinstance(body, dict):
            raise InvalidRequest("body must be a JSON object")
    status, payload = _remote(document, method, path, body=body, session=args.session)
    print(json.dumps({"status": status, "body": payload}, indent=2))
    return 0 if 200 <= status < 300 else 1


def _remote(
    document: dict[str, Any],
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    session: str | None = None,
) -> tuple[int, Any]:
    plugin, connection, _hint = connect_invitation(document)
    headers = {"Authorization": f"Bearer {session}"} if session else None
    try:
        return http_exchange(connection, method, path, json_body=body, headers=headers)
    finally:
        connection.close()
        plugin.close()


def _invitation_document(value: str) -> dict[str, Any]:
    try:
        return decode_invitation(value)
    except ValueError as exc:
        text = str(exc)
        if text == "unsupported invitation version":
            raise UnsupportedVersion(text) from exc
        raise InvalidRequest(text) from exc


def _is_remote(value: str) -> bool:
    text = value.strip()
    return text.startswith("commonroom://") or text.startswith("{")


def _emit(payload: Any, as_json: bool, render) -> int:
    if as_json:
        print(json.dumps(payload, indent=2))
        return 0
    text = render(payload)
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def _status_text(payload: dict[str, Any]) -> str:
    listener = payload.get("listener") or {}
    kind = listener.get("type") or "local"
    mode = listener.get("mode") or "local"
    encrypted = "encrypted" if listener.get("encrypted") else "not encrypted"
    return (
        f"workspace={payload['workspace_id']} version={payload['version']} path={payload['path']}\n"
        f"transport={kind} ({mode}, {encrypted})\n"
    )


def _people_text(items: list[dict[str, Any]]) -> str:
    if not items:
        return "(none)\n"
    return "".join(f"{item['kind']} {item['name']} {item['participant_id']}\n" for item in items)


def _leases_text(items: list[dict[str, Any]]) -> str:
    if not items:
        return "(none)\n"
    return "".join(
        f"{item['lease_id']} {item['target']} {item['participant_id']}\n" for item in items
    )


def _proposals_text(items: list[dict[str, Any]]) -> str:
    if not items:
        return "(none)\n"
    return "".join(
        f"{item['proposal_id']} {item['status']} base={item['base_version']} {item['summary']}\n"
        for item in items
    )


def _history_text(items: list[dict[str, Any]]) -> str:
    return "".join(
        f"{item['event_id']} {item['event_type']} {item['created_at']}\n" for item in items
    )


if __name__ == "__main__":
    raise SystemExit(main())
