"""Replies gpt-oss-120b actually wrote in the first scenario run (2026-11-24), kept as regression
cases. Four of them failed checks only because of typography: a narrow no-break space in a name,
a non-breaking hyphen in "check-in", markdown bold around "not", Swedish words."""

import pytest

from evals.scenarios import language, mentions

SEATS = "The cheapest offer (289 EUR) does not provide seat selection – seats will be assigned at check‑in."
BAGS = "The cheapest offer (289 EUR) does **not** include a checked bag."
FAILED_SV = (
    "Bokningsförsöket misslyckades – flygbolaget avslog beställningen (fel: *invalid_order*). "
    "Inga biljetter har bokats. Vill du försöka med ett annat erbjudande eller ett annat datum?"
)
TIMEOUT_SV = "Tyvärr är vår flygsökning för tillfället otillgänglig. Jag kan försöka igen om du vill."
COVER_SV = "- **C0013 – Johan Berg** (standby STBY)\n- **C0100 – Maria Wikström** (ledig OFF)"


@pytest.mark.parametrize(
    ("reply", "pattern"),
    [
        (SEATS, r"check-?in"),
        (BAGS, r"not include"),
        (FAILED_SV, r"misslyck|inga"),
        (TIMEOUT_SV, r"otillg[äa]nglig"),
        (COVER_SV, r"Johan Berg"),
    ],
)
def test_typography_no_longer_hides_the_answer(reply: str, pattern: str) -> None:
    assert mentions(reply, pattern)


def test_language_guess() -> None:
    assert language(FAILED_SV) == "sv" and language(TIMEOUT_SV) == "sv"
    assert language(SEATS) == "en" and language(BAGS) == "en"
    assert language("OK.") == "en"
