"""Compare the observed locking failure with its fix in disposable source copies."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[3]
OUTPUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from app.core.config import load_env_file
load_env_file()
dsn = os.environ.get("TEST_MYSQL_DSN")
if not dsn:
    raise SystemExit("Dedicated TEST_MYSQL_DSN required")
with zipfile.ZipFile(ROOT / "reports/system_improvement/release_checks_ticket_context_pages_final/source_snapshot.zip") as archive:
    source = {name: archive.read(name) for name in archive.namelist()}
with zipfile.ZipFile(ROOT / "reports/system_improvement/release_checks_ticket_context_pages_fixed/source_snapshot.zip") as archive:
    old = archive.read("app/services/refund_cancellation.py")
manifest = {"kind": "isolated_mysql_lock_regression", "checks": []}
method = "test_mysql_approval_holding_review_does_not_deadlock_cancellation_scan"
race = "test_approval_racing_cancellation_cannot_reactivate_the_refund"
for label, implementation, cases in [
    ("old_locking", old, [method]),
    ("primary_locking", source["app/services/refund_cancellation.py"], [method] + [race] * 20),
]:
    with tempfile.TemporaryDirectory(prefix="support-lock-diagnosis-") as tmp:
        sandbox = Path(tmp)
        for name, content in source.items():
            target = sandbox / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (sandbox / "app/services/refund_cancellation.py").write_bytes(implementation)
        env = {**os.environ, "CONSISTENCY_MYSQL_DSN": dsn, "TEST_MYSQL_DSN": "", "MYSQL_DSN": "",
               "DATABASE_BACKEND": "sqlite", "REDIS_URL": "", "REDIS_HOST": "", "AUTH_TOKENS": "",
               "PAYMENT_ADAPTER": "", "LLM_API_KEY": "", "SEED_DEMO_DATA": "false",
               "PYTHONPATH": str(sandbox), "PYTHONIOENCODING": "utf-8"}
        code = ("import sys, unittest; sys.path.insert(0, 'tests'); "
                "from test_refund_cancellation import RefundCancellationTests as Case; "
                f"suite=unittest.TestSuite(Case(name) for name in {cases!r}); "
                "result=unittest.TextTestRunner(verbosity=2).run(suite); sys.exit(not result.wasSuccessful())")
        with (OUTPUT / (label + ".txt")).open("x", encoding="utf-8") as log:
            result = subprocess.run([sys.executable, "-X", "utf8", "-c", code], cwd=sandbox,
                                    env=env, stdout=log, stderr=subprocess.STDOUT, timeout=180)
        manifest["checks"].append({"name": label, "returncode": result.returncode,
                                  "cases": cases, "implementation_sha256": hashlib.sha256(implementation).hexdigest()})
manifest["regression_confirmed"] = manifest["checks"][0]["returncode"] != 0 and manifest["checks"][1]["returncode"] == 0
(OUTPUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
print(json.dumps(manifest))
