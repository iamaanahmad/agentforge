"""Local maintenance only. Restore never overwrites a database or starts workers."""

import fcntl
import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from pydantic import ValidationError
from .config import Settings
from .db import Database


class MaintenanceError(RuntimeError):
    pass


def init_env(destination):
    path = Path(destination)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("A4G_ADMIN_PASSWORD=" + secrets.token_urlsafe(32) + "\n")
        stream.write("A4G_SESSION_SECRET=" + secrets.token_urlsafe(48) + "\n")
        stream.write("A4G_SECURE_COOKIES=false\nA4G_PUBLIC_ORIGIN=http://localhost:8000\n")
    return {
        "created": str(path),
        "mode": "local",
        "action": "Keep this file private. Use HTTPS and secure cookies in production.",
    }


def configuration():
    try:
        settings = Settings()
    except ValidationError as exc:
        # Never render input values or arbitrary validator exception reprs.
        errors = []
        for error in exc.errors(include_input=False, include_context=False, include_url=False):
            location = ".".join(str(x) for x in error["loc"])
            errors.append(
                {"field": "A4G_" + location.upper() if location else "configuration", "message": error["msg"]}
            )
        return None, {"valid": False, "errors": errors}
    except (ValueError, OSError):
        return None, {
            "valid": False,
            "errors": [{"field": "secret files", "message": "Check A4G_*_FILE paths and permissions"}],
        }
    errors, warnings = [], []
    if settings.credential_key_file:
        from .credentials import keyring

        try:
            keyring(settings.credential_key_file, settings.data_dir)
        except Exception:
            errors.append(
                {
                    "field": "A4G_CREDENTIAL_KEY_FILE",
                    "message": "Check the private keyring format, permissions and location",
                }
            )
    if not any(
        (
            settings.openai_api_key,
            settings.anthropic_api_key,
            settings.bedrock_credentials,
            settings.vertex_credentials,
        )
    ):
        warnings.append("No environment model key; check vault and /api/v1/readiness before starting work")
    for name in ("browser_socket", "sandbox_socket"):
        path = getattr(settings, name)
        if path and not Path(path).is_socket():
            errors.append({"field": "A4G_" + name.upper(), "message": "Configured broker socket is missing"})
    if not settings.secure_cookies:
        warnings.append("Local cookie mode; use HTTPS and secure cookies for production")
    return settings, {
        "valid": not errors,
        "backend": "postgresql" if settings.database_url else "sqlite",
        "errors": errors,
        "warnings": warnings,
    }


def sqlite_info(source):
    source = Path(source).resolve()
    if not source.is_file():
        raise MaintenanceError("Source database does not exist")
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > 12:
            raise MaintenanceError("Database is newer than this release; upgrade Agent4Good first")
        if (
            conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or conn.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise MaintenanceError("Database integrity check failed; preserve the source for inspection")
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"tasks", "settings", "tool_runs", "approvals"} <= names:
            raise MaintenanceError("Source is not an Agent4Good database")
        return {"schema_version": version, "current_schema_version": 11, "integrity": "ok"}


def backup_sqlite(source, destination):
    sqlite_info(source)
    destination = Path(destination).resolve()
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        with (
            sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True) as src,
            sqlite3.connect(destination) as dst,
        ):
            src.backup(dst)
        result = sqlite_info(destination)
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())
        return {**result, "backup": str(destination)}
    except Exception:
        destination.unlink(missing_ok=True)
        raise


@contextmanager
def offline_worker(settings):
    # Same lock as the daemon. The owner must also stop the web service before maintenance.
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with (settings.data_dir / "worker.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MaintenanceError("Stop the worker before migration or restoration") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def migrate(settings, backup=None):
    if settings.database_url:
        raise MaintenanceError(
            "Use migrate-postgres for SQLite transfer; PostgreSQL upgrades run on service startup"
        )
    source = settings.data_dir / "agent4good.sqlite3"
    with offline_worker(settings):
        if source.exists():
            if not backup:
                raise MaintenanceError(
                    "Provide --backup with a new private file before upgrading an existing database"
                )
            backup_sqlite(source, backup)
        Database(source)
        return sqlite_info(source)


def restore_sqlite(settings, source):
    if settings.database_url:
        raise MaintenanceError("Use restore-postgres for a PostgreSQL backup")
    destination = settings.data_dir / "agent4good.sqlite3"
    with offline_worker(settings):
        result = backup_sqlite(source, destination)
        # Restore on a new host must not revive authenticated sessions or stale readiness.
        with sqlite3.connect(destination) as conn:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM settings WHERE key='worker_last_seen'")
        return {
            **result,
            "action": "Keep services stopped; inspect receipts, schedules and credentials before starting workers",
            "sessions_revoked": True,
            "workers_started": False,
        }


def postgres_operation(settings, operation, source=None, destination=None):
    if not settings.database_url:
        raise MaintenanceError("Configure PostgreSQL and object storage for this command")
    from .postgres import PostgresDatabase
    from .transfer import backup_postgres, restore_postgres, migrate_sqlite

    db = PostgresDatabase(settings)
    if operation == "backup-postgres":
        return backup_postgres(db, destination)
    if operation == "restore-postgres":
        result = restore_postgres(db, source)
    else:
        result = migrate_sqlite(source, destination, db)
    db.execute("DELETE FROM sessions")
    db.execute("DELETE FROM settings WHERE key='worker_last_seen'")
    return {"tables": result, "sessions_revoked": True, "workers_started": False}
