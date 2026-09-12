"""Snapshot databases and restore only to a new file or new schema."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile

import pymysql
from app.core.config import load_env_file
from app.storage.mysql_database import parse_mysql_dsn


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def write_manifest(directory, backend, source_info):
    payload = directory / ("snapshot.sqlite" if backend == "sqlite" else "snapshot.sql")
    manifest = {"format": "support-database-backup-v1", "backend": backend,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "bytes": payload.stat().st_size, "sha256": digest(payload),
                "source": source_info, "status": "complete"}
    with (directory / "manifest.json").open("x", encoding="utf-8") as output:
        json.dump(manifest, output, ensure_ascii=False, indent=2)
    return manifest


def verify_backup(directory, backend):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("format") != "support-database-backup-v1" or manifest.get("backend") != backend
            or manifest.get("status") != "complete"):
        raise ValueError("Backup format/backend/status mismatch")
    payload = directory / ("snapshot.sqlite" if backend == "sqlite" else "snapshot.sql")
    if payload.stat().st_size != manifest["bytes"] or digest(payload) != manifest["sha256"]:
        raise ValueError("Backup integrity check failed")
    return payload


def sqlite_snapshot(source_path, directory):
    source_path = Path(source_path).resolve()
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
    target = sqlite3.connect(directory / "snapshot.sqlite")
    try:
        source.backup(target)  # Includes committed WAL data in a consistent image.
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite backup integrity check failed")
    finally:
        target.close()
        source.close()
    return write_manifest(directory, "sqlite", {"sqlite_version": sqlite3.sqlite_version})


def sqlite_restore(directory, destination):
    payload = verify_backup(directory, "sqlite")
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as output, payload.open("rb") as source:
        shutil.copyfileobj(source, output)
    restored = sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)
    try:
        if restored.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Restored SQLite integrity check failed")
    finally:
        restored.close()
    return {"backend": "sqlite", "destination": str(destination), "restored": True}


def connect_mysql(options, *, database=True):
    return pymysql.connect(host=options["host"], port=options["port"], user=options["user"],
                           password=options["password"], database=options["database"] if database else None,
                           charset=options["charset"], connect_timeout=options["connect_timeout"], autocommit=True)


@contextmanager
def mysql_options(options):
    # Never put a password in argv, stdout or the backup directory.
    with tempfile.TemporaryDirectory(prefix="support-mysql-client-") as tmp:
        path = Path(tmp) / "client.cnf"
        def quoted(value):
            return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") + '"'
        path.write_text("[client]\n" + "\n".join(f"{key}={quoted(options[key])}" for key in ("host", "port", "user", "password")) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        yield path


def run_mysql_binary(name, options, arguments, *, stdin=None, stdout=None):
    binary = shutil.which(name)
    if not binary:
        raise RuntimeError(f"{name} must be installed and on PATH")
    with mysql_options(options) as defaults:
        result = subprocess.run([binary, "--defaults-file=" + str(defaults), *arguments],
                                stdin=stdin, stdout=stdout or subprocess.DEVNULL, stderr=subprocess.PIPE,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        # Client stderr can contain SQL/data: don't emit it into public logs.
        raise RuntimeError(f"{name} failed with exit code {result.returncode}; backup/restore incomplete")


def mysql_snapshot(dsn, directory):
    options = parse_mysql_dsn(dsn)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", options["database"]):
        raise ValueError("Unsupported source database identifier")
    with connect_mysql(options) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT VERSION()")
            version = cursor.fetchone()[0]
            cursor.execute("SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s", (options["database"],))
            tables = cursor.fetchall()
            if not tables or any(engine != "InnoDB" for _, engine in tables):
                raise ValueError("Consistent snapshot requires a nonempty InnoDB-only schema")
            for table, column in (("ROUTINES", "ROUTINE_SCHEMA"), ("EVENTS", "EVENT_SCHEMA"), ("TRIGGERS", "TRIGGER_SCHEMA")):
                cursor.execute(f"SELECT COUNT(*) FROM information_schema.{table} WHERE {column} = %s", (options["database"],))
                if cursor.fetchone()[0]:
                    raise ValueError("This project backup supports tables only; stored programs need a separate backup strategy")
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    with (directory / "snapshot.sql").open("xb") as output:
        run_mysql_binary("mysqldump", options, ["--single-transaction", "--quick", "--skip-lock-tables",
            "--set-gtid-purged=OFF", "--no-tablespaces", "--hex-blob", "--column-statistics=0",
            "--default-character-set=utf8mb4", options["database"]], stdout=output)
    return write_manifest(directory, "mysql", {"schema": options["database"], "version": version,
                                              "tables": len(tables), "mode": "single_transaction_innodb"})


def mysql_restore(directory, dsn, new_database):
    payload = verify_backup(directory, "mysql")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", new_database):
        raise ValueError("Invalid new database identifier")
    if not shutil.which("mysql"):
        raise RuntimeError("mysql client must be installed and on PATH")
    options = parse_mysql_dsn(dsn)
    with connect_mysql(options, database=False) as connection:
        with connection.cursor() as cursor:
            # Deliberately no IF NOT EXISTS: a prior database must never be overwritten.
            cursor.execute(f"CREATE DATABASE `{new_database}` CHARACTER SET utf8mb4")
    with payload.open("rb") as source:
        run_mysql_binary("mysql", options, ["--binary-mode", "--default-character-set=utf8mb4",
                                           "--database=" + new_database], stdin=source)
    return {"backend": "mysql", "database": new_database, "restored": True}


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("action", choices=("backup", "restore"))
    parser.add_argument("--backend", required=True, choices=("sqlite", "mysql"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--sqlite-path", type=Path)
    parser.add_argument("--dsn-env", help="Environment variable name; never provide the DSN itself")
    parser.add_argument("--new-database", help="Restore creates this NEW MySQL schema; never replaces one")
    args = parser.parse_args()
    load_env_file()
    if args.backend == "sqlite":
        if args.sqlite_path is None:
            parser.error("--sqlite-path is required")
        if args.action == "backup":
            result = sqlite_snapshot(args.sqlite_path, args.directory)
        else:
            result = sqlite_restore(args.directory, args.sqlite_path)
    else:
        if not args.dsn_env or not os.getenv(args.dsn_env):
            parser.error("--dsn-env must name a configured environment variable")
        dsn = os.environ[args.dsn_env]
        if args.action == "backup":
            result = mysql_snapshot(dsn, args.directory)
        else:
            if not args.new_database:
                parser.error("--new-database is required for restore")
            result = mysql_restore(args.directory, dsn, args.new_database)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
