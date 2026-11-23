"""The "never say confirmed" guard for assistant replies.

An assistant may only tell a user a booking is confirmed (booked, ticketed, all set...) when the
booking's state, derived from the order itself, is CONFIRMED. check_reply() finds claims of a
completed booking in a draft reply. If any booking mentioned isn't confirmed, the reply is
blocked and replaced with the safe sentence for its real state.

Step 6's agent runs every draft reply through this. The tests here prove the rule holds for
many phrasings, in English and Swedish, including negations that must NOT be blocked.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .state import Booking, BookingState

# Words that claim the booking is done.
_CLAIMS = re.compile(
    r"\b(confirmed|confirmation\s+number|booked|ticketed|e-?tickets?\s+(?:issued|sent)|reserved|"
    r"all\s+set|you'?re\s+(?:going|flying)|bokad|bokat|bekr[äa]ftad|bekr[äa]ftat|biljett\s+utf[äa]rdad)\b",
    re.IGNORECASE,
)
# Words just before a claim that turn it into the opposite ("not confirmed", "awaiting confirmation").
_NEGATION = re.compile(
    r"(\bnot\b|n't\b|\bno\b|\bnothing\b|\bnever\b|\buntil\b|\bawaiting\b|\bpending\b|\bbefore\b|\byet\s+to\b|"
    r"\bwill\b|\bwould\b|\bcould\b|\bcan\b|\bmight\b|\bingenting\b|\bkommer\b|"
    r"\bunless\b|\bif\b|\bonce\b|\bwhen\b|\binte\b|\binnan\b|\bonly\b|\bwhether\b|\bun)[\w\s,']{0,30}$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReplyCheck:
    ok: bool
    claims: tuple[str, ...]
    safe_reply: str | None = None


def find_claims(reply: str) -> list[str]:
    claims = []
    for m in _CLAIMS.finditer(reply):
        before = reply[max(0, m.start() - 40) : m.start()]
        sentence_start = max(before.rfind("."), before.rfind("!"), before.rfind("?"), before.rfind("\n"))
        if not _NEGATION.search(before[sentence_start + 1 :]):
            claims.append(m.group(0))
    return claims


def check_reply(reply: str, bookings: Iterable[Booking]) -> ReplyCheck:
    """Block a reply that claims a booking is done unless every booking involved is confirmed."""
    bookings = list(bookings)
    claims = tuple(find_claims(reply))
    if not claims:
        return ReplyCheck(True, ())
    unconfirmed = [b for b in bookings if b.state is not BookingState.CONFIRMED]
    if bookings and not unconfirmed:
        return ReplyCheck(True, claims)
    if not bookings:
        safe = "I have no booking on record for this, so nothing has been booked."
    else:
        safe = " ".join(b.what_to_tell_the_user for b in unconfirmed)
    return ReplyCheck(False, claims, safe)
