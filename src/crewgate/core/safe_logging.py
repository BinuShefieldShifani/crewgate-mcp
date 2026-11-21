"""Structured JSON logs that are safe to ship to a log platform (GDPR data minimisation).

Two kinds of treatment:
- Secrets (tokens, passwords, client secrets, Authorization headers, JWTs anywhere in text)
  are replaced by "[REDACTED]". There is never a reason to log them.
- Personal data (names, emails, phone numbers, passport numbers, birth dates, crew and
  passenger ids) is replaced by a keyed pseudonym such as "pii:3f9a1c2b". The same person
  always gets the same pseudonym, so you can still follow one person's requests through the
  logs, but without the key nobody can turn it back into a name.

Redaction runs on the message, on `extra` fields and inside nested dicts and lists.
"""

import hashlib
import hmac
import json
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr

REDACTED = "[REDACTED]"

SECRET_KEYS = frozenset(
    {
        "password",
        "secret",
        "client_secret",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "subject_token",
        "authorization",
        "cookie",
        "set-cookie",
        "api_key",
        "x-crewgate-signature",
        "x-duffel-signature",
        "card_number",
        "cvc",
    }
)
PII_KEYS = frozenset(
    {
        "name",
        "first_name",
        "last_name",
        "given_name",
        "family_name",
        "email",
        "phone",
        "phone_number",
        "born_on",
        "date_of_birth",
        "passport",
        "passport_number",
        "identity_document",
        "address",
        "crew_id",
        "requester_id",
        "partner_id",
        "passenger_id",
        "username",
        "preferred_username",
    }
)

_JWT = re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]*")
_BEARER = re.compile(r"(?i)\bbearer\s+[\w.~+/-]+=*")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_CREW_ID = re.compile(r"\bC\d{4}\b")
_PHONE = re.compile(r"(?<![\w-])\+\d[\d ()-]{7,}\d")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")

_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {"message", "asctime"}


class Redactor:
    def __init__(self, pseudonym_key: SecretStr) -> None:
        self._key = pseudonym_key.get_secret_value().encode()

    def pseudonym(self, value: str) -> str:
        return "pii:" + hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()[:12]

    def text(self, value: str) -> str:
        value = _JWT.sub(REDACTED, value)
        value = _BEARER.sub(f"Bearer {REDACTED}", value)
        value = _CARD.sub(REDACTED, value)
        value = _EMAIL.sub(lambda m: self.pseudonym(m.group(0).lower()), value)
        value = _PHONE.sub(lambda m: self.pseudonym(re.sub(r"\D", "", m.group(0))), value)
        return _CREW_ID.sub(lambda m: self.pseudonym(m.group(0)), value)

    def value(self, key: str | None, value: Any) -> Any:
        k = (key or "").lower()
        if k in SECRET_KEYS:
            return REDACTED
        if isinstance(value, SecretStr):
            return REDACTED
        if isinstance(value, Mapping):
            return {str(sk): self.value(str(sk), sv) for sk, sv in value.items()}
        if isinstance(value, list | tuple | set | frozenset):
            return [self.value(key, v) for v in value]
        if k in PII_KEYS and value is not None:
            return self.pseudonym(str(value))
        if isinstance(value, str):
            return self.text(value)
        return value


class JsonFormatter(logging.Formatter):
    """One JSON object per line, redacted. Unknown `extra` fields are kept (after redaction)."""

    def __init__(self, redactor: Redactor, service: str = "crewgate") -> None:
        super().__init__()
        self.redactor, self.service = redactor, service

    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "service": self.service,
            "msg": self.redactor.text(record.getMessage()),
        }
        for key, val in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                doc[key] = self.redactor.value(key, val)
        if record.exc_info:
            doc["exc"] = self.redactor.text(self.formatException(record.exc_info))
        return json.dumps(doc, default=str, separators=(",", ":"))


def configure_logging(pseudonym_key: SecretStr, level: str = "INFO", service: str = "crewgate") -> Redactor:
    """Install the JSON formatter on the root logger. Returns the redactor for reuse (e.g. audit)."""
    redactor = Redactor(pseudonym_key)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(redactor, service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    return redactor
