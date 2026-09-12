"""Isolated operational rollback drill with demo SQLite and an injected bad image.

Uses only labelled containers/volume created by this invocation. No production
deployment, real model calls, or payments. The supplied image is retained.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from uuid import uuid4


def docker(*arguments, input=None):
    result = subprocess.run(["docker", *arguments], input=input, text=True, capture_output=True, encoding="utf-8")
    if result.returncode:
        raise RuntimeError("Docker operation failed: " + arguments[0])
    return result.stdout.strip()


def inspect(kind, name):
    return json.loads(docker(kind, "inspect", name))[0]


def request(url, token=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=2) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def wait_ready(name, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = inspect("container", name)
        if not info["State"]["Running"]:
            return None
        mappings = info["NetworkSettings"]["Ports"].get("8012/tcp") or []
        if mappings:
            endpoint = "http://127.0.0.1:" + mappings[0]["HostPort"]
            try:
                if request(endpoint + "/ready")[0] == 200:
                    return endpoint
            except (OSError, ValueError):
                pass
        time.sleep(.5)
    return None


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    ident = uuid4().hex
    label = "support.rollback-drill=" + ident
    volume = "support-drill-data-" + ident
    bad_tag = "support-agent:injected-failure-" + ident
    original_image = inspect("image", args.image)["Id"]
    containers = []
    result = {"success": False, "scope": "isolated_sqlite_operational_rollback_not_production_schema_compatibility",
              "previous_image_id": original_image, "checks": {}, "created_resources_removed": False}
    token = secrets.token_hex(32)
    started = time.monotonic()
    volume_created = False
    bad_image_created = False
    try:
        with tempfile.TemporaryDirectory(prefix="support-container-drill-") as tmp:
            tmp = Path(tmp)
            env = tmp / "runtime.env"
            env.write_text("\n".join([
                "DATABASE_BACKEND=sqlite", "DATABASE_PATH=/var/lib/support/app.db", "SEED_DEMO_DATA=true",
                "REDIS_URL=", "REDIS_HOST=", "MYSQL_DSN=", "MYSQL_USER=", "MYSQL_DATABASE=", "MQ_BACKEND=sqlite",
                "RAG_EMBEDDING_PROVIDER=local", "EMBEDDING_DIMENSIONS=256", "PAYMENT_ADAPTER=",
                "LLM_API_KEY=smoke-configuration-only-no-model-calls",
                "AUTH_TOKENS=" + json.dumps({token: {"user_id": "u009", "role": "customer"}}),
            ]), encoding="utf-8")
            os.chmod(env, 0o600)
            docker("volume", "create", "--label", label, volume)
            volume_created = True
            def start(image, suffix):
                name = "support-drill-" + suffix + "-" + ident
                docker("run", "-d", "--name", name, "--label", label, "--env-file", str(env),
                       "-v", volume + ":/var/lib/support", "-p", "127.0.0.1::8012", image)
                containers.append(name)
                return name, wait_ready(name)
            name, endpoint = start(original_image, "before")
            if endpoint is None:
                raise RuntimeError("Baseline image did not become ready")
            result["checks"]["initial_ready"] = True
            assert request(endpoint + "/orders/10009")[0] == 401
            assert request(endpoint + "/orders/10001", token)[0] == 403
            assert request(endpoint + "/orders/10009", token)[0] == 200
            result["checks"]["authorization"] = True
            bootstrap = "\n".join([
                "from app.tools.refund import refund_apply",
                "from app.services.refund_service import process_refund_tasks",
                "assert refund_apply('10009', '订单10009我要退款').success",
                "assert process_refund_tasks()['success']",
            ])
            docker("exec", "-i", name, "python", "-", input=bootstrap)
            before = request(endpoint + "/orders/10009", token)[1]["data"]
            assert before["after_sales_status"] == "refund_processing"
            docker("stop", "--time", "15", name)
            # A distinct immutable candidate image with a failing process entry.
            (tmp / "Dockerfile").write_text("FROM " + args.image + "\nLABEL " + label +
                '\nCMD ["python", "-c", "raise SystemExit(23)"]\n', encoding="utf-8")
            (tmp / ".dockerignore").write_text("*\n!Dockerfile\n", encoding="utf-8")
            docker("build", "-t", bad_tag, str(tmp))
            bad_image_created = True
            result["rejected_image_id"] = inspect("image", bad_tag)["Id"]
            bad_name, bad_endpoint = start(bad_tag, "rejected")
            assert bad_endpoint is None
            result["checks"]["failed_candidate_rejected"] = True
            restored_name, restored_endpoint = start(original_image, "restored")
            assert restored_endpoint is not None
            assert inspect("container", restored_name)["Image"] == original_image
            after = request(restored_endpoint + "/orders/10009", token)[1]["data"]
            assert after == before
            check_queue = "\n".join([
                "from app.storage.database import list_mq_messages_from_db",
                "from app.services.refund_service import process_refund_tasks",
                "assert all(m['status']=='done' for m in list_mq_messages_from_db())",
                "assert process_refund_tasks()['processed'] == 0",
            ])
            docker("exec", "-i", restored_name, "python", "-", input=check_queue)
            result["checks"].update(previous_image_restored=True, business_state_preserved=True, no_replayed_completed_event=True)
            result["success"] = True
    except Exception as error:
        result["error_type"] = type(error).__name__
        raise
    finally:
        for name in containers:
            info = inspect("container", name)
            if info["Config"]["Labels"].get("support.rollback-drill") != ident:
                raise RuntimeError("Refusing to remove an unowned container")
            docker("rm", "-f", name)
        if volume_created:
            info = inspect("volume", volume)
            if info["Labels"].get("support.rollback-drill") != ident:
                raise RuntimeError("Refusing to remove an unowned volume")
            docker("volume", "rm", volume)
        if bad_image_created:
            docker("image", "rm", bad_tag)
        result["created_resources_removed"] = True
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        (args.output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
