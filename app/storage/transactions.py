"""Explicit units of work shared by the SQLite and MySQL repositories.

Repository context managers borrow the connection; only the outer unit commits.
State is thread-local because DB connections must not migrate to tool threads.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import sqlite3
from threading import local

_local = local()


@dataclass
class TransactionState:
    connection: object
    mysql: bool
    rollback_only: bool = False
    invalidate_keys: set[str] = field(default_factory=set)


def current_transaction():
    return getattr(_local, "transaction", None)


class ManagedSQLiteConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class BorrowedConnection:
    def __init__(self, state):
        self.state = state

    def __getattr__(self, name):
        return getattr(self.state.connection, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type:
            self.state.rollback_only = True
        return False

    def commit(self):
        raise RuntimeError("Only the outer transaction may commit")

    def rollback(self):
        self.state.rollback_only = True

    def close(self):
        pass


def borrowed_connection(mysql=False):
    state = current_transaction()
    if state is None:
        return None
    if state.mysql != mysql:
        raise RuntimeError("Cannot mix database backends in a transaction")
    return BorrowedConnection(state)


@contextmanager
def transaction():
    from app.storage import database as db
    parent = current_transaction()
    if parent is not None:
        try:
            yield parent
        except BaseException:
            parent.rollback_only = True
            raise
        return
    db.ensure_database()
    mysql = db.using_mysql_backend()
    if mysql:
        from app.storage.mysql_database import get_mysql_connection
        connection = get_mysql_connection(autocommit=False)
    else:
        connection = db.get_connection()
        connection.execute("BEGIN IMMEDIATE")
    state = TransactionState(connection, mysql)
    _local.transaction = state
    committed = False
    try:
        yield state
        if state.rollback_only:
            raise RuntimeError("Transaction aborted by a nested repository failure")
        connection.commit()
        committed = True
    except BaseException:
        connection.rollback()
        raise
    finally:
        _local.transaction = None
        connection.close()
        if committed:
            for key in state.invalidate_keys:
                db._cache_delete(key)


def execute(sql, parameters=(), *, fetch=False):
    state = current_transaction()
    if state is None:
        raise RuntimeError("SQL requires an explicit transaction")
    cursor = state.connection.cursor()
    try:
        cursor.execute(sql.replace("?", "%s") if state.mysql else sql, parameters)
        return cursor.fetchall() if fetch else cursor.rowcount
    finally:
        cursor.close()


def lock_record(table, key, value):
    if (table, key) not in {("refund_requests", "refund_id"), ("mq_messages", "message_id"), ("orders", "order_id"), ("manual_reviews", "review_id"), ("tickets", "ticket_id")}:
        raise ValueError("Unsupported transaction lock")
    state = current_transaction()
    suffix = " FOR UPDATE" if state.mysql else ""
    rows = execute(f"SELECT * FROM {table} WHERE {key} = ?{suffix}", (value,), fetch=True)
    return dict(rows[0]) if rows else None
