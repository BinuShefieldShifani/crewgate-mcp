"""Check an audit file's HMAC chain.

    uv run python scripts/verify_audit.py var/audit.jsonl

The key comes from CREWGATE_AUDIT_KEY (dev default: dev-only-audit-key).
Exit code 0 means intact, 1 means tampered or unreadable.
"""

import os
import sys
from pathlib import Path

from pydantic import SecretStr

from crewgate.core.audit import verify_chain


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    key = SecretStr(os.getenv("CREWGATE_AUDIT_KEY", "dev-only-audit-key"))
    check = verify_chain(Path(sys.argv[1]).read_text().splitlines(), key)
    if check.ok:
        print(f"OK: {check.records} records, chain intact")
        return 0
    print(f"TAMPERED: record {check.first_bad_seq}: {check.problem} ({check.records} records read)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
