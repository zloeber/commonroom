from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import uvicorn

from commonroom.engine import WorkspaceEngine
from commonroom.http import create_app


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="commonroom")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Serve workspace over HTTP")
    serve.add_argument("path", help="Path to workspace markdown file")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    status = subparsers.add_parser("status", help="Get workspace status")
    status.add_argument("--workspace", default=os.getenv("COMMONROOM_WORKSPACE", "workspace.md"))
    status.add_argument("--json", action="store_true")

    invite = subparsers.add_parser("invite", help="Create invitation")
    invite.add_argument("--workspace", default=os.getenv("COMMONROOM_WORKSPACE", "workspace.md"))
    invite.add_argument("--permissions", default="read_write")
    invite.add_argument("--target")
    invite.add_argument("--expires", type=int, default=60)
    invite.add_argument("--max-uses", type=int, default=1)
    invite.add_argument("--json", action="store_true")

    join = subparsers.add_parser("join", help="Join with invitation")
    join.add_argument("invite", help="Invitation token")
    join.add_argument("--workspace", default=os.getenv("COMMONROOM_WORKSPACE", "workspace.md"))
    join.add_argument("--kind", default="agent", choices=["human", "agent"])
    join.add_argument("--name", default="participant")
    join.add_argument("--capabilities", default="")
    join.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        engine = WorkspaceEngine(args.path)
        app = create_app(engine)
        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    workspace = Path(args.workspace).expanduser().resolve()
    engine = WorkspaceEngine(workspace)
    try:
        if args.command == "status":
            payload = engine.status()
            if args.json:
                print(json.dumps(payload, indent=2))
            else:
                print(
                    f"workspace={payload['workspace_id']} version={payload['version']} path={payload['path']}"
                )
            return 0

        if args.command == "invite":
            payload = engine.create_invite(
                permissions=args.permissions,
                target=args.target,
                expires_in_minutes=args.expires,
                max_uses=args.max_uses,
            )
            if args.json:
                print(json.dumps(payload, indent=2))
            else:
                print(payload["token"])
            return 0

        if args.command == "join":
            capabilities = [item for item in args.capabilities.split(",") if item]
            payload = engine.join(
                kind=args.kind,
                name=args.name,
                capabilities=capabilities,
                invite_token=args.invite,
            )
            if args.json:
                print(json.dumps(payload, indent=2))
            else:
                print(payload["participant_id"])
            return 0
    finally:
        engine.close()

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
