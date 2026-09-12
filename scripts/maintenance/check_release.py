"""Run source checks in a disposable copy with no business credentials."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from time import perf_counter
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def source_files():
    for folder in ("app", "scripts", "tests", "web", "data/knowledge"):
        for path in sorted((ROOT / folder).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                yield path
    for name in ("main.py", "requirements.txt", "data/orders.json", "Dockerfile", ".dockerignore"):
        yield ROOT / name


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mysql", action="store_true", help="Also require TEST_MYSQL_DSN and MySQL backup clients")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    mysql_dsn = ""
    if args.mysql:
        from app.core.config import load_env_file
        load_env_file()
        mysql_dsn = os.getenv("TEST_MYSQL_DSN", "")
    # Freeze bytes before running any suite; exclude .env, eval datasets and backups.
    files = {p.relative_to(ROOT).as_posix(): p.read_bytes() for p in source_files()}
    manifest = {"source_files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
                "python": sys.version, "checks": [], "mysql_requested": args.mysql,
                "packages": {dist.metadata["Name"]: dist.version for dist in importlib.metadata.distributions() if dist.metadata["Name"]}}
    with zipfile.ZipFile(output / "source_snapshot.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    with tempfile.TemporaryDirectory(prefix="support-release-check-") as tmp:
        sandbox = Path(tmp)
        for name, data in files.items():
            path = sandbox / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        env = {**os.environ, "DATABASE_BACKEND": "sqlite", "DATABASE_PATH": str(sandbox / "runtime.db"),
               "MYSQL_DSN": "", "MYSQL_USER": "", "MYSQL_DATABASE": "", "REDIS_URL": "", "REDIS_HOST": "",
               "AUTH_TOKENS": "", "PAYMENT_ADAPTER": "", "SEED_DEMO_DATA": "false",
               "LLM_API_KEY": "", "ZHIPUAI_API_KEY": "", "ZHIPU_API_KEY": "", "BIGMODEL_API_KEY": "",
               "CONSISTENCY_MYSQL_DSN": "", "TEST_MYSQL_DSN": "", "RAG_EMBEDDING_PROVIDER": "local",
               "EMBEDDING_DIMENSIONS": "256", "PYTHONPATH": str(sandbox), "PYTHONIOENCODING": "utf-8"}
        checks = [("compile", ["-m", "compileall", "-q", "app", "main.py", "scripts"], None),
                  ("unit_integration", ["-m", "pytest", "tests", "-q", "--tb=short"], None)]
        if args.mysql and mysql_dsn:
            checks.append(("mysql_integration", ["-m", "pytest", "tests/test_refund_consistency.py",
                          "tests/test_database_backup.py", "tests/test_operational_observability.py",
                          "tests/test_refund_cancellation.py", "tests/test_nonrefund_review_continuation.py",
                          "tests/test_review_resolution.py", "tests/test_payment_reconciliation.py",
                          "tests/test_ticket_lifecycle.py", "tests/test_human_handoff.py", "tests/test_refund_followup.py",
                          "tests/test_refund_worker.py", "-q", "--tb=short"], mysql_dsn))
        for name, command, test_dsn in checks:
            started = perf_counter()
            check_env = {**env, "CONSISTENCY_MYSQL_DSN": test_dsn or ""}
            with (output / (name + ".txt")).open("x", encoding="utf-8") as log:
                result = subprocess.run([sys.executable, "-X", "utf8", *command], cwd=sandbox,
                                        env=check_env, stdout=log, stderr=subprocess.STDOUT)
            manifest["checks"].append({"name": name, "returncode": result.returncode,
                                       "duration_seconds": round(perf_counter() - started, 3)})
        if args.mysql and not mysql_dsn:
            manifest["checks"].append({"name": "mysql_integration", "status": "dependency_unavailable", "returncode": 2})
    manifest["success"] = all(check["returncode"] == 0 for check in manifest["checks"])
    with (output / "manifest.json").open("x", encoding="utf-8") as target:
        json.dump(manifest, target, ensure_ascii=False, indent=2)
    print(json.dumps({"success": manifest["success"], "checks": manifest["checks"], "output": str(output)}, ensure_ascii=False))
    return 0 if manifest["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
