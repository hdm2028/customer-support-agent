"""Use a disposable Redis container and fresh MySQL schemas for fault checks."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
from time import monotonic, sleep
from uuid import uuid4
import zipfile

from scripts.maintenance.check_release import ROOT, source_files
from app.core.config import load_env_file


def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=True, timeout=45)
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    load_env_file()
    dsn = os.getenv("TEST_MYSQL_DSN")
    if not dsn:
        raise SystemExit("TEST_MYSQL_DSN required; no business database fallback")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    files = {p.relative_to(ROOT).as_posix(): p.read_bytes() for p in source_files()}
    with zipfile.ZipFile(output / "source_snapshot.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    image = docker("image", "inspect", "redis:7.4-alpine", "--format", "{{.Id}}")
    marker = uuid4().hex
    container = None
    manifest = {"source_files": {n: hashlib.sha256(b).hexdigest() for n, b in files.items()},
                "redis_image": image, "mysql": "fresh support_test_* schema per test", "success": False}
    try:
        # Explicit binding keeps the same endpoint across stop/start fault tests.
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            reserved_port = reservation.getsockname()[1]
        container = docker("run", "-d", "--name", "support-redis-check-" + marker,
                           "--label", "support.test=" + marker, "-p", f"127.0.0.1:{reserved_port}:6379", image,
                           "redis-server", "--save", "", "--appendonly", "no")
        details = json.loads(docker("inspect", container))[0]
        port = details["NetworkSettings"]["Ports"]["6379/tcp"][0]["HostPort"]
        url = f"redis://127.0.0.1:{port}/0?socket_timeout=1&socket_connect_timeout=1"
        import redis
        probe = redis.Redis.from_url(url)
        deadline = monotonic() + 15
        while True:
            try:
                probe.ping()
                break
            except redis.RedisError:
                if monotonic() >= deadline:
                    raise
                sleep(.1)
        manifest.update(redis_version=probe.info("server")["redis_version"], port=int(port))
        probe.close()
        with tempfile.TemporaryDirectory(prefix="support-redis-check-") as tmp:
            sandbox = Path(tmp)
            for name, content in files.items():
                target = sandbox / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            env = {**os.environ, "CONSISTENCY_MYSQL_DSN": dsn, "CONSISTENCY_REDIS_URL": url,
                   "DISPOSABLE_REDIS_CONTAINER": container, "DISPOSABLE_REDIS_MARKER": marker,
                   "MYSQL_DSN": "", "TEST_MYSQL_DSN": "", "DATABASE_BACKEND": "sqlite",
                   "REDIS_URL": "", "REDIS_HOST": "", "AUTH_TOKENS": "", "PAYMENT_ADAPTER": "",
                   "LLM_API_KEY": "", "PYTHONPATH": str(sandbox), "PYTHONIOENCODING": "utf-8"}
            started = monotonic()
            with (output / "tests.txt").open("x", encoding="utf-8") as log:
                result = subprocess.run([sys.executable, "-X", "utf8", "-m", "pytest",
                                         "tests/test_redis_consistency.py", "-q", "-rA", "--tb=short"],
                                        cwd=sandbox, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=240)
            manifest.update(returncode=result.returncode, duration_seconds=round(monotonic() - started, 3),
                            success=result.returncode == 0)
    finally:
        if container:
            details = json.loads(docker("inspect", container))[0]
            if details["Id"] != container or details["Config"]["Labels"].get("support.test") != marker:
                raise RuntimeError("Refusing to remove a container outside this test run")
            docker("rm", "-f", container)
            manifest["container_removed"] = True
        (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in manifest.items() if k != "source_files"}, ensure_ascii=False))
    return 0 if manifest["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
