// crewgate console. No framework, no build step. All text goes in via textContent.
"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
const post = (url, body) =>
  fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json", "x-crewgate": "1" },
    body: JSON.stringify(body || {}),
  });

const day = new Date(Date.now() + 30 * 864e5).toISOString().slice(0, 10);
const SUGGESTIONS = [
  "Who could cover a captain at Gothenburg on 2026-12-05?",
  `Find flights from Arlanda (ARN) to Heathrow (LHR) on ${day}.`,
  `Book the cheapest flight from ARN to LHR on ${day} for Anna Berg (Ms, female, born 1990-04-02, anna@example.com, +46701234567).`,
  "Is my flight booked now?",
  "Propose giving C0005's duty on 6 December 2026 to C0010. Reason: training clash.",
];

let turns = [];

async function load() {
  const r = await fetch("/api/session");
  const s = await r.json();
  $("note").textContent = s.note || "";
  if (s.grafana_url) { $("grafana").href = s.grafana_url; $("grafana").hidden = false; }
  const err = new URLSearchParams(location.search).get("error");
  if (err) { $("signin-error").textContent = "Sign-in problem: " + err.replaceAll("_", " "); $("signin-error").hidden = false; }
  if (!s.signed_in) { $("signed-out").hidden = false; return; }
  $("signed-in").hidden = false;
  $("who").textContent = s.username; $("who").hidden = false; $("logout").hidden = false;
  turns = s.log || [];
  turns.forEach(addTurn);
  limits(s);
  suggestions();
  if (s.chat_enabled === false) { $("limits").textContent = "Chat is off on this server (no model key)."; $("send").disabled = true; }
  if (turns.length) select(turns.length - 1);
}

function limits(s) {
  const parts = [];
  if (s.turns_left !== undefined) parts.push(`${s.turns_left} messages left this hour`);
  if (s.model_calls_left_today !== undefined) parts.push(`${s.model_calls_left_today} model calls left today`);
  $("limits").textContent = parts.join(" · ");
}

function suggestions() {
  const box = $("suggestions");
  box.replaceChildren();
  SUGGESTIONS.forEach((t) => {
    const b = el("button", "chip", t.length > 60 ? t.slice(0, 57) + "..." : t);
    b.type = "button"; b.title = t;
    b.onclick = () => { $("input").value = t; $("input").focus(); };
    box.append(b);
  });
}

function addTurn(t, i) {
  const msgs = $("messages");
  msgs.append(el("div", "msg user", t.user));
  const a = el("div", "msg agent", t.reply);
  const tag = el("span", "tag" + (t.guard.ok ? "" : " replaced"),
    t.guard.ok ? `${t.steps.length} steps · ${t.seconds}s` : "Guard replaced the model's draft · see steps");
  a.append(tag);
  const idx = i === undefined ? turns.length - 1 : i;
  a.onclick = () => select(idx);
  a.dataset.idx = idx;
  msgs.append(a);
  msgs.scrollTop = msgs.scrollHeight;
}

function select(i) {
  document.querySelectorAll(".msg.agent").forEach((n) => n.classList.toggle("selected", Number(n.dataset.idx) === i));
  const t = turns[i];
  $("trace-hint").hidden = true;
  const list = $("steps");
  list.replaceChildren();
  const step = (cls, title, detail) => {
    const li = el("li", "step " + cls);
    li.append(el("b", null, title));
    if (detail) li.append(el("pre", null, detail));
    list.append(li);
  };
  t.steps.forEach((s) => {
    if (s.kind === "model") {
      const wants = s.wants.length ? `wants ${s.wants.join(", ")}` : "writes a reply";
      const who = s.model ? ` · ${s.model}` : "";
      step("model", `Model ${wants}`, `${s.tokens} tokens · ${s.ms} ms${who}${s.retried ? " · retried a malformed tool call" : ""}`);
    } else if (s.kind === "tool") {
      step(s.error ? "tool err" : "tool", `Gateway: ${s.tool}${s.error ? " refused (" + s.error + ")" : ""}`,
        JSON.stringify(s.arguments, null, 1) + `\n${s.ms} ms`);
    } else if (s.kind === "blocked") {
      step("blocked", `Agent stopped ${s.tool}`, "You didn't ask for this change, so it was never sent.");
    } else if (s.kind === "rewrite") {
      step("rewrite", "Reply rewritten into your language", s.ok ? "" : "Rewrite failed; original kept.");
    }
  });
  if (t.guard.ok) step("guard-ok", "Guard: reply matches the facts", "");
  else step("guard-replaced", "Guard replaced the draft", `The model wrote:\n${t.guard.draft}`);

  const ap = $("approvals");
  ap.replaceChildren();
  t.approvals.forEach((a) => {
    const box = el("div", "box");
    box.append(el("div", null, "Waiting for a person to approve:"));
    box.append(el("div", "small", a.summary || a.tool));
    const link = el("a", null, "Open the approval page");
    link.href = a.approve_url; link.target = "_blank"; link.rel = "noopener";
    box.append(link);
    ap.append(box);
  });
  const bk = $("bookings");
  bk.replaceChildren();
  t.bookings.forEach((b) => {
    const box = el("div", "box" + (b.confirmed ? " ok" : ""));
    box.textContent = `Booking ${b.id}: ${b.state}${b.reference ? " · reference " + b.reference : ""}`;
    bk.append(box);
  });
}

async function send(ev) {
  ev.preventDefault();
  const text = $("input").value.trim();
  if (!text) return;
  $("send").disabled = true; $("send").textContent = "Working...";
  try {
    const r = await post("/api/chat", { message: text });
    const body = await r.json();
    if (r.status === 401) { location.href = "/login"; return; }
    if (!r.ok) { alert(body.message || body.error || "Something went wrong."); return; }
    turns.push(body);
    addTurn(body);
    select(turns.length - 1);
    limits(body);
    $("input").value = "";
  } finally {
    $("send").disabled = false; $("send").textContent = "Send";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("form").addEventListener("submit", send);
  $("input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) send(e); });
  $("reset").onclick = async () => { await post("/api/reset"); location.reload(); };
  $("logout").onclick = async () => {
    const r = await post("/logout");
    const body = await r.json();
    location.href = body.logout_url || "/";
  };
  load();
});
