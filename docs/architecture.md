# Architecture

Commonroom is a workspace engine with replaceable transports.

```text
Human / agent
      │
Commonroom protocol
      │
Workspace engine  (markdown file + SQLite metadata)
      │
Session and capabilities
      │
Transport API
      │
loopback plugin, or an entry-point plugin such as Tailcat
```

The engine does not import a transport implementation. Plugins register on `commonroom.transports` and satisfy `listen`, `connect`, `accept`, `send`, `receive`, and `close`. An invitation is `commonroom://join/<opaque>` and carries transport hints inside the opaque blob. Dialing a hint does not create a session. `POST /session/join` does, and the invitation's capabilities bound that session.

Tailcat is the first private plugin. It is not part of workspace identity. Loopback is the in-tree plugin used for development and the conformance tests.

The human GUI and the agent API read the same SQLite state. The GUI listens on loopback and is not the port a private plugin exposes.

See `AGENTS.md` for the invariants to keep when changing this.
