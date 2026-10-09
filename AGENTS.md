# AGENTS.md

This file is the map for anyone changing Commonroom. Read it before adding a feature.

## What Commonroom is

Commonroom turns something a person already has on their computer into a temporary room. Another person, or an agent acting for them, can enter that room, see who is there, reserve a piece of the work, and propose changes. The host file stays the host's file.

The experience to protect:

```bash
commonroom ./idea.md
```

A room opens. The host copies one `commonroom://join/...` capability to an agent. The agent connects, joins, observes, leases a region, proposes, and commits only after a person approves. Nobody needs a Commonroom account, a Tailscale account, a VPN, or a central server.

If a change does not make that interaction simpler, do not make it.

## Do not build

Commonroom is not a hosted collaboration suite, a chat product, a git host, a project tracker, an agent framework, a VPN, a general filesystem share, or an identity platform. A future hosted relay would be another transport plugin. It would not be a second workspace engine.

## Invariants

These are the rules that keep the room one thing:

- The substrate file is the artifact. SQLite stores collaboration metadata. Do not move the user's file and do not make SQLite the document.
- Version numbers only increase. Never silently overwrite or rebase. A stale commit returns `stale_version` with `current_version`, `diff`, and `changes`.
- Humans and agents use the same operations and the same state. The GUI is a projection of that state, not a side channel.
- A transport connection is not admission. Join with an invitation, then send the session on later requests.
- Invitations, sessions, participants, workspaces, and transport endpoints are different objects. Restarting the process may change the endpoint. It must not change the workspace id.
- Remote participants do not receive the host's filesystem. Serving `./idea.md` does not expose `./` or `../`.
- Do not put invitation secrets, session ids, or transport endpoints in events or logs. Diagnostics may name the plugin (`loopback`, `tailcat`) and whether it is encrypted. They must not echo the endpoint address.
- The copyable artifact is `commonroom://join/<opaque>`. Do not make `tailcat://`, a peer id, or a relay URL the thing a person hands to an agent.

## Transport boundary

Everything above the transport API is Commonroom. Everything below it is replaceable.

```text
workspace, sessions, capabilities, proposals, leases, events, GUI, HTTP
                              │
                       Transport API
                              │
            loopback (in tree)    plugins (entry points)
```

The engine, HTTP adapter, protocol document, and GUI must not import or name a transport vendor. `tests/test_transport.py` fails if they do.

A plugin implements:

```text
listen() -> endpoint hint
connect(hint) -> connection
accept() -> connection
connection.send(bytes) / connection.receive() / close()
close()
capabilities()
```

`expose(port)` is the extra method a plugin needs when the HTTP server is already listening and the plugin only has to carry bytes to that port. Tailcat does this. Loopback does this by dialing the port directly.

Plugins register on the entry point group `commonroom.transports`:

```toml
[project.entry-points."commonroom.transports"]
loopback = "commonroom.transport.loopback:plugin"
tailcat = "commonroom.transport.plugins.tailcat:plugin"
```

The host selects one by name (`--transport tailcat` or `--tailcat`). The invitation carries opaque hints:

```json
{"type": "tailcat", "address": "...", "port": 1234, "encrypted": true, "mode": "private"}
```

`connect_invitation` tries hints in order and uses the first installed plugin. That is the whole negotiation mechanism. Do not add a second negotiator.

### Adding a transport later

Iroh, libp2p, WebRTC, a managed relay, or anything else is a new plugin. Add it only when it solves a real Commonroom problem. To add one:

1. Create `src/commonroom/transport/plugins/<name>.py` with the same methods as loopback.
2. Register an entry point. Do not import it from `engine.py`, `http.py`, `protocol.py`, `gui.py`, or `serving.py`.
3. Run the conformance tests in `tests/test_transport.py` against it. The tests must not grow a branch for the plugin's brand.
4. Put endpoint material only in the invitation hint. Session rows store the plugin name the client reports, not the secret address.

Tailcat is the first private plugin. It shells out to the `tailcat` binary (`serve --key=new`, `forward`). Commonroom does not implement Tailcat's cryptography. If the binary is missing, local loopback still works and `--tailcat` fails with `transport_unavailable`.

Loopback is the reference plugin and the conformance implementation. It is local TCP, not a fake of Tailcat's protocol.

### What a plugin must not do

- Decide capabilities, create sessions, or read the workspace.
- Become required for `commonroom ./idea.md`.
- Open a second channel for file transfer. Artifact bytes are workspace operations on the same connection.

## Package map

```text
src/commonroom/engine.py          collaboration state and SQLite
src/commonroom/http.py            HTTP adapter, session required
src/commonroom/gui.py             human projection of that state
src/commonroom/protocol.py        operations, errors, invitation codec
src/commonroom/serving.py         binds loopback HTTP and asks a plugin to expose it
src/commonroom/client.py          HTTP over a transport connection
src/commonroom/cli.py             host commands; names the tailcat plugin only here
src/commonroom/transport/         interface, loopback, registry
src/commonroom/transport/plugins/ optional plugins
skills/commonroom/SKILL.md        the agent skill; keep it true
```

`Workspace` in Python is `WorkspaceEngine`. Embedded apps use `create_app(workspace)`, which defaults to session access and does not start a transport.

Two HTTP listeners exist when a private plugin is on:

- `127.0.0.1:<port>` is the human GUI. It sets an HttpOnly cookie. It is not what the plugin exposes.
- Another loopback port is the ingress. The plugin forwards that port. It has no owner cookie. Callers need `Authorization: Bearer <session>`.

Do not merge those listeners. A plugin that forwards the GUI port would publish the owner cookie to anyone who can fetch `/`.

## Collaboration rules already implemented

- Default agent invitation: `read`, `observe`, `propose`, `checkout`, `commit`. Not `approve`, `invite`, or `admin`.
- The host session is `*`.
- HTTP identity comes from the session. Body fields do not choose the actor.
- Raw document commit over HTTP requires `admin`. Agents commit an approved `proposal_id`.
- Leases last 15 minutes by default, 120 at most, and expire on their own. The holder can renew inside that window.
- Markdown regions are heading paths. `section:Goals` and `Architecture/Goals` both work.
- External edits to the markdown file become a new version. The file wins over SQLite.

## Errors

Return JSON `{error, message, details}` and the recovery fields agents need (`current_version`, `diff`, `changes`). Codes live in `commonroom.protocol.ERROR_CODES`. Add a code there when you add a failure mode. Do not make agents parse sentences.

## Commands

```bash
mise install
task install
task check
task test
task serve
```

`task check` is format, lint, compile, and tests. CI should call the same steps.

```bash
commonroom ./idea.md
commonroom ./idea.md --tailcat
commonroom serve ./idea.md --transport <plugin>
commonroom invite --workspace ./idea.md
commonroom join 'commonroom://join/...' --name 'Agent A' --no-hold
commonroom call 'commonroom://join/...' GET /workspace/status --session '<session_id>'
```

`--ephemeral` keeps metadata in a temp directory and deletes it on exit. The markdown file remains.

## Tests that matter

- `tests/test_transport.py` is the conformance suite. New plugins should pass it.
- `tests/test_remote.py` is a participant dialing the invitation through the transport API, not through the engine object.
- `tests/test_http.py` checks that the ingress rejects anonymous calls and that an agent cannot approve or raw-commit.
- `tests/test_skill.py` fails when `skills/commonroom/SKILL.md` drifts from the protocol.

A real Tailcat smoke test needs the `tailcat` binary and network. Do not make `task check` depend on it. `COMMONROOM_TAILCAT` can point at the binary.

## When you are unsure

Prefer the smaller change that keeps the room local, the file user-owned, and the transport swappable. Leave Iroh, libp2p, WebRTC, and hosted relays unimplemented until a concrete joining problem shows up that Tailcat and loopback cannot cover.
