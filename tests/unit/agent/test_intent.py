"""The intent gate: a write tool runs only when the user's latest message asks for it."""

from crewgate.agent.intent import asked_for


def user(text: str) -> dict[str, str]:
    return {"role": "user", "content": text}


def agent(text: str) -> dict[str, str]:
    return {"role": "assistant", "content": text}


def test_read_tools_always_pass() -> None:
    assert asked_for("booking_search_flights", [user("Show me flights to London")])


def test_booking_needs_a_request_to_book() -> None:
    assert asked_for("booking_book_flight", [user("Book the cheapest flight for Anna Berg.")])
    assert asked_for("booking_book_flight", [user("Boka det billigaste flyget åt mig")])
    assert not asked_for("booking_book_flight", [user("Show me flights from ARN to LHR on Friday.")])
    assert not asked_for("booking_book_flight", [])


def test_yes_to_the_agents_own_question_counts() -> None:
    history = [
        user("Show me flights"),
        agent("The fare is 301 EUR now. Shall I book it?"),
        user("Yes please"),
    ]
    assert asked_for("booking_book_flight", history)
    history = [user("Show me flights"), agent("Here are three flights."), user("Yes")]
    assert not asked_for("booking_book_flight", history)


def test_only_the_latest_user_message_counts() -> None:
    history = [
        user("Book a flight to London"),
        agent("Booked request sent."),
        user("What about the weather?"),
    ]
    assert not asked_for("booking_book_flight", history)


def test_swaps() -> None:
    assert asked_for("crew_propose_swap", [user("Propose giving C0005's duty on 6 December to C0010.")])
    assert asked_for("crew_propose_swap", [user("Swap C0042 with C0107 on the 12th")])
    assert not asked_for("crew_propose_swap", [user("Who could cover a captain at Gothenburg?")])
