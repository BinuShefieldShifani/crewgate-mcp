"""Tamper-evident audit log: every tool call and every refusal, chained with HMAC-SHA256.

Each record holds the HMAC of the previous record. Changing, deleting or reordering any line
breaks every link after it, and `verify_chain` reports the first broken sequence number.
The chain is keyed, so someone who can edit the file but doesn't hold the key can't rebuild
a valid chain after tampering.

What is recorded: who (user id and client app), what (tool, decision, reason, outcome), when,
and a SHA-256 digest of the arguments. Raw arguments are not stored: they can hold personal
data, and the digest is enough to prove later which exact request was made.
"""

import hashlib
import hmac
import json
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import SecretStr

GENESIS = "0" * 64


class Decision(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    PENDING_APPROVAL = "pending_approval"


@dataclass(frozen=True)
class AuditRecord:
    seq: int
    ts: float
    subject: str
    client_id: str
    tool: str
    decision: str
    reason: str
    outcome: str  # "ok", "error:<category>", or "-" for refusals
    args_sha256: str
    duration_ms: float
    prev: str
    mac: str = ""

    def body(self) -> bytes:
        d = asdict(self)
        d.pop("mac")
        return json.dumps(d, sort_keys=True, separators=(",", ":")).encode()


def args_digest(args: Any) -> str:
    return hashlib.sha256(
        json.dumps(args, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


class AuditSink(Protocol):
    def write(self, line: str) -> None: ...


class MemorySink:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, line: str) -> None:
        self.lines.append(line)


class FileSink:
    """Append-only JSON lines. fsync per record: an audit trail is worthless if it can be lost."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, line: str) -> None:
        import os

        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())


class AuditLog:
    def __init__(self, key: SecretStr, sink: AuditSink, clock: Callable[[], float] = time.time) -> None:
        self._key = key.get_secret_value().encode()
        self.sink, self.clock = sink, clock
        self._lock = threading.Lock()
        self._seq = 0
        self._head = GENESIS

    @property
    def head(self) -> str:
        """Latest MAC. Publishing it elsewhere (a log line, a ticket) anchors the chain."""
        return self._head

    def record(
        self,
        *,
        subject: str,
        client_id: str,
        tool: str,
        decision: Decision,
        reason: str,
        outcome: str,
        args: Any,
        duration_ms: float = 0.0,
    ) -> AuditRecord:
        with self._lock:
            self._seq += 1
            rec = AuditRecord(
                seq=self._seq,
                ts=round(self.clock(), 3),
                subject=subject,
                client_id=client_id,
                tool=tool,
                decision=decision.value,
                reason=reason,
                outcome=outcome,
                args_sha256=args_digest(args),
                duration_ms=round(duration_ms, 2),
                prev=self._head,
            )
            mac = _mac(self._key, rec.body())
            rec = AuditRecord(**{**asdict(rec), "mac": mac})
            self.sink.write(json.dumps(asdict(rec), sort_keys=True, separators=(",", ":")))
            self._head = mac
            return rec


def _mac(key: bytes, body: bytes) -> str:
    return hmac.new(key, body, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class ChainCheck:
    ok: bool
    records: int
    first_bad_seq: int | None = None
    problem: str | None = None


def _parse(lines: Iterable[str]) -> Iterator[AuditRecord]:
    for line in lines:
        if line.strip():
            yield AuditRecord(**json.loads(line))


def verify_chain(lines: Iterable[str], key: SecretStr) -> ChainCheck:
    k = key.get_secret_value().encode()
    prev, expected_seq, count = GENESIS, 1, 0
    try:
        for rec in _parse(lines):
            count += 1
            if rec.seq != expected_seq:
                return ChainCheck(False, count, rec.seq, f"expected seq {expected_seq}")
            if rec.prev != prev:
                return ChainCheck(False, count, rec.seq, "link to previous record broken")
            if not hmac.compare_digest(_mac(k, rec.body()), rec.mac):
                return ChainCheck(False, count, rec.seq, "record was modified")
            prev, expected_seq = rec.mac, expected_seq + 1
    except (ValueError, TypeError) as exc:
        return ChainCheck(False, count, expected_seq, f"unreadable record: {type(exc).__name__}")
    return ChainCheck(True, count)
