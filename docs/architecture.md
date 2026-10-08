# Commonroom Architecture (Bootstrap)

Commonroom is implemented as a library-first workspace engine.

- **Engine** (`src/commonroom/engine.py`) owns workspace semantics:
  - workspace lifecycle metadata
  - participants
  - leases
  - proposals
  - invitations
  - optimistic concurrency
  - event history
- **Substrate**: Markdown file remains user-owned on local filesystem.
- **Metadata**: SQLite stores collaboration state and versions.
- **Adapters**:
  - CLI (`src/commonroom/cli.py`)
  - HTTP + GUI (`src/commonroom/http.py`)

This keeps transport concerns separate from collaboration semantics so additional adapters (MCP, Tailcat ingress, other transports) can map to the same operations.
