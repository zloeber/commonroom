from __future__ import annotations

OWNER_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Commonroom</title>
  <style>
    :root { color-scheme: light; --ink: #1c1915; --muted: #5c564c; --line: #e4ddd2; --paper: #f7f4ee; --card: #fffdf9; --green: #0f6b4c; --red: #8f3d32; }
    * { box-sizing: border-box; }
    body { margin: 0; font: 16px/1.45 "Iowan Old Style", Palatino, Georgia, serif; color: var(--ink); background: var(--paper); }
    main { max-width: 920px; margin: 0 auto; padding: 32px 20px 64px; }
    h1 { font-size: 2rem; margin: 0; }
    h2 { font-size: 1.05rem; margin: 0 0 10px; }
    p { color: var(--muted); }
    section { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px 18px; margin: 14px 0; }
    ul { list-style: none; padding: 0; margin: 0; }
    li { padding: 8px 0; border-top: 1px solid var(--line); }
    li:first-child { border-top: 0; }
    button { font: inherit; border: 1px solid var(--line); background: white; border-radius: 8px; padding: 6px 10px; margin-right: 6px; cursor: pointer; }
    button.approve { color: var(--green); }
    button.reject { color: var(--red); }
    pre { white-space: pre-wrap; word-break: break-all; background: #f3efe7; padding: 10px; border-radius: 8px; }
    .meta { color: var(--muted); font-size: 0.92rem; }
    #notice { min-height: 1.2em; }
  </style>
</head>
<body>
<main>
  <header>
    <h1 id="workspace-name">Commonroom</h1>
    <p>Version <strong id="version">Loading</strong> · <span id="lifecycle"></span></p>
    <p id="notice" class="meta"></p>
  </header>
  <section>
    <h2>Connection</h2>
    <p id="connection" class="meta">Loading</p>
  </section>
  <section>
    <h2>Participants</h2>
    <ul id="participants"></ul>
  </section>
  <section>
    <h2>Leases</h2>
    <ul id="leases"></ul>
  </section>
  <section>
    <h2>Proposals</h2>
    <div id="proposals"></div>
  </section>
  <section>
    <h2>Invitation</h2>
    <button id="create-invite" type="button">Create invitation</button>
    <pre id="invite-uri"></pre>
  </section>
  <section>
    <h2>Outline</h2>
    <ul id="outline"></ul>
  </section>
  <section>
    <h2>Recent events</h2>
    <ul id="events"></ul>
  </section>
</main>
<script>
const notice = document.getElementById("notice");
let rendered = "";
function text(id, value) { document.getElementById(id).textContent = value; }
function empty(node, message) {
  node.replaceChildren();
  const item = document.createElement("li");
  item.textContent = message;
  node.append(item);
}
async function call(path, options) {
  const response = await fetch(path, {credentials: "same-origin", ...options});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.message || body.error || response.statusText);
  return body;
}
async function review(id, approve) {
  notice.textContent = "";
  try {
    await call("/workspace/proposal/review", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({proposal_id: id, approve}),
    });
    await load();
  } catch (error) {
    notice.textContent = error.message;
  }
}
async function commitProposal(id) {
  notice.textContent = "";
  try {
    await call("/workspace/commit", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({proposal_id: id}),
    });
    await load();
  } catch (error) {
    notice.textContent = error.message;
  }
}
function renderProposals(proposals) {
  const root = document.getElementById("proposals");
  root.replaceChildren();
  if (!proposals.length) {
    root.textContent = "No proposals yet.";
    return;
  }
  for (const proposal of proposals) {
    const card = document.createElement("div");
    card.style.padding = "8px 0";
    card.style.borderTop = "1px solid var(--line)";
    const title = document.createElement("strong");
    title.textContent = proposal.summary;
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = proposal.status + " · base v" + proposal.base_version + " · " + proposal.target;
    card.append(title, meta);
    if (proposal.status === "pending") {
      const approve = document.createElement("button");
      approve.type = "button";
      approve.className = "approve";
      approve.textContent = "Approve";
      approve.addEventListener("click", () => review(proposal.proposal_id, true));
      const reject = document.createElement("button");
      reject.type = "button";
      reject.className = "reject";
      reject.textContent = "Reject";
      reject.addEventListener("click", () => review(proposal.proposal_id, false));
      card.append(approve, reject);
    }
    if (proposal.status === "approved") {
      const commit = document.createElement("button");
      commit.type = "button";
      commit.textContent = "Commit";
      commit.addEventListener("click", () => commitProposal(proposal.proposal_id));
      card.append(commit);
    }
    root.append(card);
  }
}
async function load() {
  const room = await call("/workspace/room");
  const fingerprint = JSON.stringify(room);
  if (fingerprint === rendered) return;
  rendered = fingerprint;
  text("workspace-name", room.status.name || "Commonroom");
  text("version", String(room.status.version));
  text("lifecycle", room.status.lifecycle || "");
  const listener = room.diagnostics.listener || {};
  const encrypted = listener.encrypted ? "encrypted" : "not encrypted";
  text("connection", (listener.type || "local") + " · " + (listener.mode || "local") + " · " + encrypted);
  const people = document.getElementById("participants");
  if (!room.participants.length) empty(people, "No one else is here.");
  else {
    people.replaceChildren();
    for (const person of room.participants) {
      const item = document.createElement("li");
      item.textContent = person.kind + ": " + person.name + (person.transport ? " · " + person.transport : "");
      people.append(item);
    }
  }
  const leases = document.getElementById("leases");
  if (!room.leases.length) empty(leases, "No active leases.");
  else {
    leases.replaceChildren();
    for (const lease of room.leases) {
      const item = document.createElement("li");
      item.textContent = lease.target + " · until " + lease.expires_at;
      leases.append(item);
    }
  }
  renderProposals(room.proposals);
  const outline = document.getElementById("outline");
  const headings = room.summary.outline || [];
  if (!headings.length) empty(outline, "No headings yet.");
  else {
    outline.replaceChildren();
    for (const heading of headings) {
      const item = document.createElement("li");
      item.textContent = "  ".repeat(Math.max(0, heading.level - 1)) + heading.path;
      outline.append(item);
    }
  }
  const events = document.getElementById("events");
  events.replaceChildren();
  for (const event of room.events) {
    const item = document.createElement("li");
    item.textContent = event.event_type + " · " + event.created_at;
    events.append(item);
  }
}
document.getElementById("create-invite").addEventListener("click", async () => {
  notice.textContent = "";
  try {
    const invite = await call("/workspace/invite", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: "{}",
    });
    document.getElementById("invite-uri").textContent = invite.uri;
  } catch (error) {
    notice.textContent = error.message;
  }
});
load().catch((error) => { notice.textContent = error.message; });
setInterval(() => load().catch(() => {}), 3000);
</script>
</body>
</html>
"""

INGRESS_PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8" /><title>Commonroom</title></head>
<body>
<main>
  <h1>Commonroom</h1>
  <p>This ingress accepts an invitation. A transport endpoint is not access.</p>
  <ul>
    <li><a href="/.well-known/agent-workspace">Discovery</a></li>
    <li><a href="/protocol">Protocol</a></li>
    <li><a href="/skills/commonroom">Skill</a></li>
  </ul>
</main>
</body>
</html>
"""
