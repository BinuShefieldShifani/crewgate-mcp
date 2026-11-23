# ADR 0006: Booking state comes from the order, bookings are never retried, and "confirmed" is earned

Status: accepted, 2026-11-21

## Context
An assistant that tells a traveller "you're booked" when the airline hasn't confirmed anything
does real harm: missed flights, double bookings, support calls. Duffel's orders have no single
status field, some orders are only accepted (HTTP 202) and settled later by webhook, prices can
change between search and booking, and Duffel documents no idempotency key for creating orders.

## Decisions

**State is derived from the order, nothing else** (`plugins/booking/state.py`):
`cancelled_at` -> cancelled; `payment_status.awaiting_payment` -> awaiting_payment; no
`booking_reference` or no electronic ticket -> pending_confirmation; reference + paid + e-ticket
-> **confirmed**, the only state where `confirmed` is true. Attempts that never became an order
get their own states: price_changed, offer_unavailable, outcome_unknown, failed.
`get_booking` re-reads the order from Duffel instead of trusting a cached "confirmed".

**The price is checked again after approval.** A person approves an exact total. Just before
booking, the gateway fetches the offer again; if the total or currency moved, nothing is booked and
the state is price_changed with the new total.

**No duplicate bookings.** The shared partner client retries only idempotent calls. A POST is
retried only when it provably never reached the partner (connection failed), after a 429, or
after a 401 refresh. A timeout after sending becomes `outcome_unknown`, which tells the user
"don't book again, it's being checked". Searches are marked safe to retry explicitly. Every order
carries `metadata.crewgate_booking_id`, so a webhook can settle even an order that had no id yet.

**Webhooks**: verified `X-Duffel-Signature`, a 5 minute timestamp window, and duplicates dropped
by `idempotency_key` (Duffel's sample code does neither of the last two). 200 for every verified
event so Duffel stops retrying, 401 for anything unverified, and nothing changes state unverified.

**The reply guard** (`confirmation.check_reply`): finds claims of a completed booking ("booked",
"confirmed", "all set", "you're flying", "e-tickets issued", "bokad", "bekräftad"...) that are not
negated ("not confirmed yet", "nothing was booked", "once it's confirmed"). If any booking involved
is not confirmed, the reply is blocked and replaced by that state's safe sentence. Tests run every
claim against every non-confirmed state. Step 6's agent sends every draft reply through it.

**Test mode only.** A `duffel_live_` token is refused when settings load, and the contract workflow
refuses anything that isn't a `duffel_test_` token.

## Consequences
- The guard is deliberately conservative: an honest sentence it misreads gets replaced by a
  correct one, never the other way round.
- Booking attempts and webhook de-duplication live in memory: one replica, or a shared store.
- Card payments, holds paid later, cancellations and airline-initiated changes beyond flagging are
  out of scope for the prototype; the state model already has room for them.
