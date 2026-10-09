# Remote acceptance

This is the check for a collaborator on another machine. Unit tests do not replace it. A Tailcat hairpin on one computer does not replace it either.

## What has to be true

The host runs Commonroom with Tailcat. The human page stays on `127.0.0.1`. Tailcat forwards only the agent ingress. The thing you send is one `commonroom://join/...` invitation. The other machine dials that hint, joins once, and uses the session after that.

Joining spends one use. A failed dial does not. A second join returns `invitation_redeemed`. Reconnect by calling again with the same session. Closing the TCP connection does not remove the participant and does not end the session. The session lasts until `workspace.leave` or it expires (about eight hours).

The invitation is a bearer secret. Anyone who can read the message can redeem it.

## Host

```bash
commonroom ./idea.md --tailcat
```

Copy the `commonroom://join/...` line only after the transport line says `READY`. If Tailcat is missing or exits, the process prints `transport_unavailable` and does not print an invitation.

The human page is the `Human:` URL. Open it to approve a proposal. That page is not what Tailcat exposes.

## Collaborator

Tailcat has to be installed. The agent should ask before installing it.

```bash
commonroom join 'commonroom://join/...' --name 'Agent B' --kind agent --json --no-hold
commonroom call 'commonroom://join/...' GET /workspace/summary --session '<session_id>'
commonroom call 'commonroom://join/...' POST /workspace/propose --session '<session_id>' --body '<json>'
```

`call` dials again. It does not join again. After the host approves in the local page, commit with the proposal id:

```bash
commonroom call 'commonroom://join/...' POST /workspace/commit --session '<session_id>' --body '{"proposal_id":"..."}'
```

## Negative checks

- Expired invite: `invitation_expired`
- Revoked invite: `invitation_revoked`
- Second join: `invitation_redeemed`
- Approve with an agent session: `permission_denied`
- Wrong or unreachable Tailcat address: `transport_unavailable`, invite unused
- `commonroom ./idea.md` with no Tailcat still serves loopback on `127.0.0.1`

`commonroom status --workspace ./idea.md` names the transport plugin and whether it is encrypted. It does not print the address.

## Recorded run

Not yet run across two machines. This milestone stays open until that happens.

On 2026-10-09, on one Mac with Homebrew `tailcat`, this passed:

```bash
COMMONROOM_TAILCAT_SMOKE=1 pytest tests/test_tailcat_smoke.py
```

That process started Commonroom with `--tailcat` equivalent (`transport_name="tailcat"`), which runs `tailcat serve --key=new` against the loopback ingress. A second process in the same test ran `commonroom join` and `commonroom call` through `tailcat forward`. The agent joined as `Agent B`, read the summary, proposed a change to `Goals`, was refused `approve`, and after the host approved on `127.0.0.1` committed `ship together`. A later `call` used the same session on a new dial and saw version 2. The listener diagnostics did not include the Tailcat address. The human URL was not the invitation target.

That is a real Tailcat tunnel, not the loopback plugin. Both sides still shared one operating system and one network stack. It does not close this milestone.
