"""The audit chain must catch any edit, deletion, insertion or reordering."""

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from crewgate.core.audit import GENESIS, AuditLog, Decision, FileSink, MemorySink, args_digest, verify_chain

KEY = SecretStr("dev-only-audit-key")


def fill(log: AuditLog, n: int = 5) -> None:
    for i in range(n):
        log.record(
            subject=f"user-{i}",
            client_id="crewgate-mcp-client",
            tool="crew.get_roster",
            decision=Decision.ALLOWED if i % 2 == 0 else Decision.DENIED,
            reason="ok" if i % 2 == 0 else "insufficient_scope",
            outcome="ok" if i % 2 == 0 else "-",
            args={"crew_id": f"C000{i}"},
            duration_ms=1.234,
        )


@pytest.fixture
def lines() -> list[str]:
    sink = MemorySink()
    fill(AuditLog(KEY, sink, clock=lambda: 1_800_000_000.0))
    return sink.lines


def test_intact_chain_verifies(lines: list[str]) -> None:
    check = verify_chain(lines, KEY)
    assert check.ok and check.records == 5
    assert json.loads(lines[0])["prev"] == GENESIS


def test_raw_arguments_are_not_stored(lines: list[str]) -> None:
    assert "C0000" not in "".join(lines)
    assert json.loads(lines[0])["args_sha256"] == args_digest({"crew_id": "C0000"})


def edit(lines: list[str], i: int, **change: object) -> list[str]:
    out = list(lines)
    rec = json.loads(out[i])
    rec.update(change)
    out[i] = json.dumps(rec)
    return out


def test_edited_record(lines: list[str]) -> None:
    # the realistic attack: turn a refusal into an allowed call
    check = verify_chain(edit(lines, 1, decision="allowed", reason="ok"), KEY)
    assert not check.ok and check.first_bad_seq == 2 and check.problem == "record was modified"


def test_deleted_record(lines: list[str]) -> None:
    check = verify_chain(lines[:2] + lines[3:], KEY)
    assert not check.ok and check.first_bad_seq == 4


def test_reordered_records(lines: list[str]) -> None:
    check = verify_chain([lines[0], lines[2], lines[1], *lines[3:]], KEY)
    assert not check.ok and check.first_bad_seq == 3


def test_rebuilt_chain_without_the_key_fails(lines: list[str]) -> None:
    forged_sink = MemorySink()
    fill(AuditLog(SecretStr("dev-only-attacker-key"), forged_sink, clock=lambda: 1_800_000_000.0))
    check = verify_chain(forged_sink.lines, KEY)
    assert not check.ok and check.first_bad_seq == 1


def test_relinked_record_is_caught(lines: list[str]) -> None:
    check = verify_chain(edit(lines, 3, prev="f" * 64), KEY)
    assert not check.ok and check.problem == "link to previous record broken"


def test_garbage_line(lines: list[str]) -> None:
    check = verify_chain([*lines[:2], "{not json", *lines[2:]], KEY)
    assert not check.ok and check.first_bad_seq == 3 and "unreadable" in str(check.problem)


def test_head_moves_with_each_record() -> None:
    log = AuditLog(KEY, MemorySink())
    assert log.head == GENESIS
    rec = log.record(
        subject="s", client_id="c", tool="t", decision=Decision.ALLOWED, reason="ok", outcome="ok", args={}
    )
    assert log.head == rec.mac != GENESIS


def test_file_sink_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "audit" / "audit.jsonl"
    fill(AuditLog(KEY, FileSink(path)), n=3)
    check = verify_chain(path.read_text().splitlines(), KEY)
    assert check.ok and check.records == 3
