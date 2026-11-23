# ADR 0007: The agent is a small LangGraph graph with no way around the guard, and tool descriptions are measured

Status: accepted, 2026-11-23

## Context
Step 5 gave the gateway a reply guard but nothing that used it. An agent that can skip the guard
on one path (an error, a step limit, a second turn) makes the guard decorative. Separately, the
text of a tool description decides which tool a model picks, and most teams tune it by feel.

## Decisions

**Graph: think, act, guard.** `think` is one model call with the gateway's tools; `act` runs the
calls through the MCP gateway and records facts; `guard` checks the draft. Every path to END goes
through `guard`, including running out of steps. The facts (booking states, pending approvals) live
in the graph state and carry over between turns, so a later "is it booked?" is checked against what
the tools said. The model's own words never count as a fact. When the guard replaces a draft, the
history keeps the replacement: what the user saw is what the model sees next turn.

**The guard only speaks when flights are in play.** It runs on every reply, but applies once a
booking tool was used or the draft talks about flights, so "Erik is confirmed on standby" passes.
A made-up "your flight is booked" with no tool call at all is still blocked.

**No agent framework beyond LangGraph, no model SDK.** The model client is ~150 lines over httpx
against any OpenAI-compatible API, so pacing, 429 handling and the stop on a daily limit are
visible and tested. The MCP side is the official SDK client over streamable HTTP. The agent holds
the user's access token and nothing else: no partner keys, no approval rights.

**Prompt size is managed, not hoped for.** pydantic's `title` annotations are dropped and `$ref`s
inlined in tool parameters (fewer tokens, and some providers reject references), tool results are
sent as compact JSON and trimmed past 3000 characters. The largest request in the scenarios is about
5,000 to 6,000 tokens with the longest descriptions, under GitHub Models' 8,000 input-token cap.

**Tool descriptions are a gateway setting with three variants** (`minimal`, `guided`,
`contract`), each complete for every tool (checked at load). A test fails if a description names a
tool by its internal dotted name (`booking.get_offer`): the model only ever sees `booking_get_offer`.
Step 5's text did exactly that; it is fixed in all variants. The variant that wins the selection
eval becomes the default, by changing one setting.

**Model evals run on GitHub Models in Actions**, not Kaggle with a local 7B model as first
planned. Reasons: small local models are weak at tool calling, which would measure the model rather
than the descriptions; GitHub Models needs no secret (the workflow token with `models: read`); and
the run sits next to the code it measures. Kaggle stays available for an open-weights comparison.

## Consequences
- The selection numbers are only as broad as 36 cases; the report prints confidence intervals and
  McNemar p-values so a small gap isn't sold as a win.
- Free-tier limits mean the two model suites run on different days.
- Also fixed on the way: the gateway never passed its audit log to plugins, so Step 5's webhook
  events were verified but not audited. They are now.
