"""Small cross-process primitives for JSON-backed stores."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

StorageSignature = tuple[int, int, int]


class PersistenceError(RuntimeError):
    """A durable state operation could not be completed reliably."""


class PersistenceCorruptionError(PersistenceError):
    """Persisted data exists but is malformed or violates its schema."""


@contextmanager
def serialized_file_update(path: str | Path, *, timeout_s: float = 30.0) -> Iterator[None]:
    """Serialize a read-modify-write cycle across Windows and Linux processes.

    SQLite supplies the process-safe lock and releases it automatically if a
    writer exits. The JSON file remains the source of truth and keeps its
    existing on-disk format.
    """
    target = Path(path)
    connection: sqlite3.Connection | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_path = target.with_name(f".{target.name}.lock.sqlite3")
        connection = sqlite3.connect(lock_path, timeout=timeout_s, isolation_level=None)
        connection.execute(f"PRAGMA busy_timeout = {int(timeout_s * 1000)}")
        connection.execute("CREATE TABLE IF NOT EXISTS file_lock (id INTEGER PRIMARY KEY)")
        connection.execute("BEGIN IMMEDIATE")
    except (OSError, sqlite3.Error) as exc:
        if connection is not None:
            with suppress(sqlite3.Error):
                connection.close()
        raise PersistenceError("persistence lock is unavailable") from exc
    assert connection is not None
    try:
        yield
    finally:
        # The transaction carries no business data; it exists only to hold
        # SQLite's cross-process write lock. Rollback releases that lock and
        # cannot turn an already replaced JSON file into a false failure.
        with suppress(sqlite3.Error):
            connection.rollback()
        with suppress(sqlite3.Error):
            connection.close()


def atomic_write_text(path: str | Path, content: str, *, encoding: str = "utf-8") -> Path:
    """Flush a same-directory temporary file and atomically replace the target."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def storage_signature(path: str | Path) -> StorageSignature | None:
    """Cheap change detector for an atomically replaced persistence file."""
    try:
        stat = Path(path).stat()
    except FileNotFoundError:
        return None
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)
