"""Manage a separate local MySQL daemon using the installed server binary.

No Windows service or business database configuration is changed. Credentials
stay in ignored local files; the test user can only manage support_test_* schemas.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import time

import pymysql


REPO = Path(__file__).resolve().parents[2]
STATE = REPO / "data/cache/local_test_mysql.json"
ROOT = Path(os.getenv("LOCALAPPDATA", str(REPO / "data/cache"))) / "customer-support-agent/test-mysql"


def connect(state, *, user="root"):
    return pymysql.connect(host="127.0.0.1", port=state["port"], user=user,
                           password=state["root_password" if user == "root" else "test_password"],
                           connect_timeout=3, read_timeout=5, autocommit=True)


def verify_instance(connection, state):
    with connection.cursor() as cursor:
        cursor.execute("SELECT @@datadir, @@port, @@server_uuid")
        datadir, port, server_uuid = cursor.fetchone()
    if Path(datadir).resolve() != Path(state["datadir"]).resolve() or port != state["port"]:
        raise RuntimeError("Refusing to operate on a different MySQL instance")
    if state.get("server_uuid") and server_uuid != state["server_uuid"]:
        raise RuntimeError("MySQL instance UUID changed")
    return server_uuid


def configure_env(state):
    path = REPO / ".env"
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    dsn = f"mysql+pymysql://support_test_runner:{state['test_password']}@127.0.0.1:{state['port']}/support_test_control"
    lines = content.splitlines()
    existing = [line for line in lines if line.strip().startswith("TEST_MYSQL_DSN=")]
    if existing and any(line.split("=", 1)[1].strip().strip('\"\'') not in ("", dsn) for line in existing):
        raise RuntimeError("TEST_MYSQL_DSN already points elsewhere; existing setting preserved")
    lines = [line for line in lines if not line.strip().startswith("TEST_MYSQL_DSN=")]
    path.write_text("\n".join(lines) + "\nTEST_MYSQL_DSN=" + dsn + "\n", encoding="utf-8")


def start(port):
    first = not STATE.exists()
    if first:
        binary = shutil.which("mysqld")
        if not binary:
            raise RuntimeError("mysqld not installed or not on PATH")
        if ROOT.exists():
            raise RuntimeError("Unmanaged test directory exists; refusing to reuse it")
        state = {"binary": binary, "datadir": str(ROOT / "data"), "port": port,
                 "root_password": secrets.token_hex(32), "test_password": secrets.token_hex(32)}
    else:
        state = json.loads(STATE.read_text(encoding="utf-8"))
        try:
            with connect(state) as connection:
                verify_instance(connection, state)
            configure_env(state)
            print(f"Dedicated test MySQL already running on 127.0.0.1:{state['port']}")
            return
        except pymysql.OperationalError as error:
            if error.args[0] != 2003:
                raise
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", state["port"]))
    ROOT.mkdir(parents=True, exist_ok=True)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    if first:
        # Save recovery credentials before any server initialization.
        STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        with (ROOT / "initialize.log").open("w", encoding="utf-8") as log:
            subprocess.run([state["binary"], "--no-defaults", "--initialize-insecure",
                            "--datadir=" + state["datadir"]], stdout=log, stderr=log,
                           check=True, creationflags=flags, timeout=120)
    init = ROOT / "bootstrap.sql"
    bootstrap = not state.get("server_uuid")
    if bootstrap:
        init.write_text(
            f"ALTER USER 'root'@'localhost' IDENTIFIED BY '{state['root_password']}';\n"
            f"CREATE USER IF NOT EXISTS 'support_test_runner'@'localhost' IDENTIFIED BY '{state['test_password']}';\n"
            "GRANT ALL PRIVILEGES ON `support\\_test\\_%`.* TO 'support_test_runner'@'localhost';\n",
            encoding="utf-8")
    command = [state["binary"], "--no-defaults", "--datadir=" + state["datadir"],
               "--bind-address=127.0.0.1", "--port=" + str(state["port"]),
               "--mysqlx=OFF", "--skip-log-bin", "--log-error=" + str(ROOT / "server.log"),
               "--pid-file=" + str(ROOT / "server.pid")]
    if bootstrap:
        command.append("--init-file=" + str(init))
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    deadline = time.monotonic() + 60
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Test MySQL exited; inspect its local server.log")
            try:
                with connect(state) as connection:
                    state["server_uuid"] = verify_instance(connection, state)
                break
            except pymysql.OperationalError:
                time.sleep(0.25)
        else:
            raise RuntimeError("Test MySQL startup timed out")
        state["pid"] = process.pid
        STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        with connect(state, user="support_test_runner") as connection:
            verify_instance(connection, state)
        configure_env(state)
    except BaseException:
        if process.poll() is None:
            process.terminate()  # Only the process created by this invocation.
            process.wait(timeout=15)
        raise
    finally:
        if init.exists():
            init.unlink()
    print(f"Dedicated test MySQL ready on 127.0.0.1:{state['port']}; TEST_MYSQL_DSN configured locally")


def stop():
    state = json.loads(STATE.read_text(encoding="utf-8"))
    with connect(state) as connection:
        verify_instance(connection, state)
        with connection.cursor() as cursor:
            cursor.execute("SHUTDOWN")
    print("Dedicated test MySQL shutdown requested; data and local configuration retained")


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument("--port", type=int, default=13307)
    args = parser.parse_args()
    if args.action == "start":
        start(args.port)
    else:
        stop()


if __name__ == "__main__":
    main()
