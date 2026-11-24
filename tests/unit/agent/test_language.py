from crewgate.agent.language import detect, mismatch, rewrite_request

SV_REPLY = "Tyvärr är vår flygsökning för tillfället otillgänglig. Jag kan försöka igen om du vill."


def test_detect() -> None:
    assert detect("Find flights from Stansted (STN) to Heathrow (LHR) on 2026-12-24.") == "en"
    assert detect(SV_REPLY) == "sv"
    assert detect("OK") is None and detect("ARN LHR 289 EUR") is None


def test_mismatch_needs_a_clear_reply() -> None:
    assert mismatch("Find flights to London on Friday", SV_REPLY) == "en"
    assert mismatch("Find flights to London on Friday", "Here you go: the flight is on Friday.") is None
    assert mismatch("Hitta flyg till London", SV_REPLY) is None
    assert mismatch("OK", SV_REPLY) is None
    assert mismatch("Find flights to London", "Hej!") is None  # too short to be sure


def test_rewrite_request_keeps_the_text() -> None:
    req = rewrite_request(SV_REPLY, "en")
    assert req.startswith("Rewrite the reply below in English") and req.endswith(SV_REPLY)
