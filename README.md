# Commonroom

Commonroom is a local-first shared workspace where humans and AI agents can collaborate in the same stateful room with leases, proposals, invitations, and optimistic concurrency.

## Why

Commonroom is a lightweight primitive for temporary collaboration over user-owned artifacts (starting with Markdown), without requiring a centralized SaaS platform.

## Install

```bash
mise install
task install
```

Or direct with `uv`:

```bash
uv sync --all-extras
```

## Standalone mode

Expose an existing Markdown file as a shared workspace:

```bash
uv run commonroom serve ./workspace.md
```

Then open `http://127.0.0.1:8000` for the human GUI.

## Join and invite

Create an invitation token:

```bash
uv run commonroom invite --workspace ./workspace.md
```

Join with the token:

```bash
uv run commonroom join <invite-token> --workspace ./workspace.md --name my-agent --kind agent
```

## Agent usage model

Agent-friendly endpoints:

- `/.well-known/agent-workspace`
- `/skills`
- `/protocol`
- `/workspace/status`
- `/workspace/summary`
- `/workspace/read`
- `/workspace/diff`
- `/workspace/changes_since`
- `/workspace/observe`

Collaboration loop:

1. observe/status/summary
2. checkout lease
3. propose
4. human review approve/reject
5. commit approved proposal

## Embedded mode

Use the same workspace engine directly from Python:

```python
from commonroom import Workspace

workspace = Workspace("./workspace.md")
status = workspace.status()
```

You can embed the engine directly, or mount the provided HTTP adapter:

```python
from commonroom.http import create_app

app = create_app(workspace)
```

## Architecture

```text
Commonroom Workspace Protocol
            |
     Workspace Engine
            |
  Markdown + SQLite metadata
            |
   CLI / HTTP / GUI adapters
```

See `docs/architecture.md` for more detail.

## Development workflow

Use Taskfile commands:

```bash
task install
task format
task lint
task test
task check
task serve
```

`task check` runs formatter check, lint, typecheck, and tests.
