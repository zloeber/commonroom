---
name: commonroom
description: Join a Commonroom as a participant. Observe the room, lease a region, propose a change, and commit only after review.
---

# Commonroom

Commonroom is a temporary room around something a person already has on their computer. You are a participant. The artifact stays on the host. You do not get a copy to overwrite.

An invitation is a secret. It looks like:

```text
commonroom://join/<opaque>
```

That string is the capability. It is not a transport address. Treat it like a password. It expires and can be revoked.

## Connect

Decode the invitation. Inside the opaque blob is JSON with:

- `protocol` and `version`
- `transports`: hints for how to reach the room
- `transport`: the first hint
- `workspace_id`
- `secret`: present this when you join; it is not a session
- `expires_at`
- `capabilities`

Use the first hint whose transport plugin you have. A hint's `type` is the plugin name (`loopback`, or a private rendezvous plugin the host started). Dial it, then speak HTTP to the Commonroom protocol on that connection.

A successful dial does not admit you to the room. Join next.

If the host gave you the `commonroom` command, dial and join, then exit so you can keep working:

```bash
commonroom join 'commonroom://join/...' --name '<your name>' --kind agent --json --no-hold
```

Keep `session_id`. Each later operation dials again. Do not join again.

```bash
commonroom call 'commonroom://join/...' GET /workspace/status --session '<session_id>'
commonroom call 'commonroom://join/...' POST /workspace/propose --session '<session_id>' --body '<json>'
```

A hint whose `type` is `tailcat` needs the `tailcat` program on your machine. If it is missing, tell your human and wait for approval before installing it. Do not turn that address into a URL, and do not guess a different endpoint.

A failed dial does not spend the invitation. Joining spends one use. Reconnect with the same `session_id` until the session expires. A second join on a spent invitation returns `invitation_redeemed`. Anyone who can read the invitation can redeem it. There is no recipient binding.

## Discover

On the connection, before you change anything:

1. `GET /.well-known/agent-workspace`
2. `GET /protocol`
3. `GET /skills/commonroom`

Prefer those documents over a remembered API. They list operations, error codes, and the transport plugin entry point group. They do not contain the artifact.

## Join

```http
POST /session/join
Content-Type: application/json

{"invite": "<secret>", "name": "<your name>", "kind": "agent", "transport": "<hint type>"}
```

Send the returned `session_id` after that:

```http
Authorization: Bearer <session_id>
```

Your capabilities are the ones in the invitation. Asking for more does nothing. When the session expires, join again with an invitation that is still valid.

## Work in this order

1. `workspace.status`
2. `workspace.summary`
3. `workspace.read` for one region, with `max_lines` or `max_bytes`
4. `workspace.checkout` for that region
5. `workspace.propose` against the `base_version` you just read
6. Wait until the proposal is `approved`
7. `workspace.commit` with the `proposal_id`
8. `workspace.release`

Do this. Do not read the whole artifact and write it back.

`workspace.commit` of a raw document requires the host capability. A normal session commits the approved proposal only.

`workspace.observe` and `workspace.history` tell you what the room did while you waited. `workspace.diagnostics` tells you which transport plugin is listening. It does not reveal the endpoint.

## Regions

Markdown regions are heading paths such as `Architecture` or `Architecture/Goals`. Checkout `section:Architecture`. Read that section instead of the file.

## Leases

A lease expires (about 15 minutes) and everyone can see it. `lease_conflict` means someone else holds that region. Read `workspace.leases`, pick another region, or wait. Renew with `workspace.renew` while you still hold it. Release when you stop.

## Conflicts

`stale_version` means the room moved. Do not retry the same body. Read `current_version`, `diff`, and `changes`. Look again, then write a new proposal.

Other codes you can act on: `unauthorized`, `permission_denied`, `invitation_expired`, `invitation_revoked`, `invitation_redeemed`, `unsupported_version`, `session_expired`, `proposal_conflict`, `invalid_region`, `transport_unavailable`.

Branch on the `error` field. Do not scrape the message.

## Etiquette

- Observe before you write.
- Lease the smallest region you need.
- Propose a summary a human can review in the room.
- Do not approve, invite, or administer unless the session says you can.
- `workspace.leave` when you are finished, and release any lease you still hold.
