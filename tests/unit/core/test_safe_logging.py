"""Logs must never carry secrets or readable personal data."""

import io
import json
import logging

import pytest
from pydantic import SecretStr

from crewgate.core.safe_logging import REDACTED, JsonFormatter, Redactor, configure_logging

KEY = SecretStr("dev-only-pseudonym-key")
# Built at runtime so no token-shaped literal sits in the repo for secret scanners to flag.
JWT = ".".join(["eyJhbGciOiJSUzI1NiJ9", "eyJzdWIiOiJhbm5hIn0", "c2lnbmF0dXJl"])


@pytest.fixture
def r() -> Redactor:
    return Redactor(KEY)


def test_pseudonyms_are_stable_keyed_and_short(r: Redactor) -> None:
    a = r.pseudonym("anna@crew.example")
    assert a == r.pseudonym("anna@crew.example") and a.startswith("pii:") and len(a) == 16
    assert a != Redactor(SecretStr("dev-only-other-key")).pseudonym("anna@crew.example")


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        (f"token {JWT} rejected", JWT),
        ("Authorization: Bearer abc.def-ghi", "abc.def-ghi"),
        ("mail anna.berg+x@crew.example now", "anna.berg"),
        ("swap C0042 with C0007", "C0042"),
        ("call +46 734 798 686", "798 686"),
        ("card 4242 4242 4242 4242 declined", "4242 4242"),
    ],
)
def test_text_redaction(r: Redactor, text: str, leak: str) -> None:
    assert leak not in r.text(text)


def test_crew_ids_map_to_the_same_pseudonym_in_text_and_fields(r: Redactor) -> None:
    assert r.pseudonym("C0042") in r.text("crew C0042")
    assert r.value("crew_id", "C0042") == r.pseudonym("C0042")


def test_structured_values(r: Redactor) -> None:
    doc = {
        "client_secret": "dev-only-gateway-secret",
        "headers": {"Authorization": f"Bearer {JWT}", "Accept": "json"},
        "passengers": [
            {"given_name": "Anna", "family_name": "Berg", "born_on": "1990-01-01", "type": "adult"}
        ],
        "requester_id": "C0001",
        "note": "ok",
        "count": 3,
        "secret_obj": SecretStr("x"),
        "email": None,
    }
    out = r.value(None, doc)
    assert out["client_secret"] == REDACTED
    assert out["headers"]["Authorization"] == REDACTED
    assert out["headers"]["Accept"] == "json"
    p = out["passengers"][0]
    assert p["type"] == "adult" and "Anna" not in json.dumps(out) and "1990" not in json.dumps(out)
    assert out["requester_id"].startswith("pii:")
    assert out["count"] == 3 and out["note"] == "ok" and out["secret_obj"] == REDACTED
    assert out["email"] is None


def test_json_formatter_end_to_end() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(Redactor(KEY)))
    log = logging.getLogger("test.safe")
    log.handlers[:] = [handler]
    log.propagate = False
    log.warning("login for anna@crew.example failed", extra={"subject_token": JWT, "tool": "crew.get_roster"})
    try:
        raise ValueError(f"boom with {JWT}")
    except ValueError:
        log.exception("crash")
    first, second = (json.loads(line) for line in stream.getvalue().splitlines())
    assert first["level"] == "warning" and first["tool"] == "crew.get_roster"
    assert first["subject_token"] == REDACTED and "anna@" not in first["msg"]
    assert JWT not in second["exc"]


def test_configure_logging_installs_json_on_root() -> None:
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        configure_logging(KEY, level="DEBUG")
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        assert root.level == logging.DEBUG
    finally:
        root.handlers[:], root.level = saved[0], saved[1]
