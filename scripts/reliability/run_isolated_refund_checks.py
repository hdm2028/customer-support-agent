"""Run fault tests against new SQLite files or freshly created MySQL schemas."""
import argparse
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--mysql", action="store_true")
    parser.add_argument("--suite", choices=("consistency", "observability", "backup", "business", "worker"), default="consistency")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    env = os.environ.copy()
    if args.mysql:
        from app.core.config import get_settings
        get_settings()  # Load local environment without logging credentials.
        dsn = os.getenv("TEST_MYSQL_DSN", "")
        if not dsn:
            raise SystemExit("TEST_MYSQL_DSN not configured; MySQL checks not executed")
        env["CONSISTENCY_MYSQL_DSN"] = dsn
    else:
        env.pop("CONSISTENCY_MYSQL_DSN", None)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as log:
        test_files = {"consistency": ["tests/test_refund_consistency.py"],
                      "worker": ["tests/test_refund_worker.py"],
                      "observability": ["tests/test_operational_observability.py"],
                      "backup": ["tests/test_database_backup.py"],
                      "business": ["tests/test_refund_cancellation.py", "tests/test_nonrefund_review_continuation.py",
                                   "tests/test_review_resolution.py", "tests/test_payment_reconciliation.py",
                                   "tests/test_ticket_lifecycle.py", "tests/test_human_handoff.py"]}[args.suite]
        result = subprocess.run([sys.executable, "-X", "utf8", "-m", "pytest", *test_files, "-q", "--tb=short", "--maxfail=1"],
                                env=env, stdout=log, stderr=subprocess.STDOUT)
    print(path.read_text(encoding="utf-8"))
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
