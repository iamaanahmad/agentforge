"""Installed `a4g` command: private owner client and explicit local maintenance."""

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

import httpx
from .sdk import Client, APIError
from . import maintenance


def parser():
    p = argparse.ArgumentParser(
        prog="a4g", description="Agent4Good v1 owner tools. Writes never automatically retry."
    )
    p.add_argument("--url", default=os.getenv("A4G_URL", "http://localhost:8000"))
    p.add_argument(
        "--password-file", help="Private owner password file; otherwise A4G_ADMIN_PASSWORD or hidden prompt"
    )
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create private local .env; refuses overwrite")
    init.add_argument("--env-file", default=".env")
    sub.add_parser("config-check", help="Validate configuration without printing secrets")
    for command in ("serve", "worker"):
        child = sub.add_parser(command)
        if command == "serve":
            child.add_argument("--host", default="127.0.0.1")
            child.add_argument("--port", type=int, default=8000)
    sub.add_parser("health", help="Authenticated readiness; exits nonzero when worker/model is unavailable")
    sub.add_parser("readiness", help="Inspect setup even before a provider key is configured")
    m = sub.add_parser("mission").add_subparsers(dest="action", required=True)
    m.add_parser("create").add_argument("file", help="Mission v1 JSON file")
    for action in ("inspect", "cancel", "start", "pause", "resume", "replan", "plan", "review"):
        child = m.add_parser(action)
        child.add_argument("id")
        if action in ("plan", "review"):
            child.add_argument("file")
    for command in ("debug", "replay"):
        child = sub.add_parser(command)
        child.add_argument("id")
        if command == "replay":
            child.add_argument("--mode", choices=["recorded"], default="recorded")
            child.add_argument("--offset", type=int, default=0)
            child.add_argument("--limit", type=int, default=100)
    e = sub.add_parser("events", help="One ordered page; pass next_cursor as --after to continue")
    e.add_argument("--after", type=int, default=0)
    e.add_argument("--through", type=int)
    e.add_argument("--task-id")
    a = sub.add_parser("approvals").add_subparsers(dest="action", required=True)
    a.add_parser("list")
    d = a.add_parser("decide")
    d.add_argument("id")
    d.add_argument("decision", choices=["approve", "reject"])
    sub.add_parser("schema")
    child = sub.add_parser("migrate", help="Stop web/worker first; initialize or upgrade SQLite")
    child.add_argument("--backup")
    sub.add_parser("schema-status")
    sub.add_parser("backup").add_argument("destination")
    sub.add_parser("restore", help="Restore into a new data directory only").add_argument("source")
    sub.add_parser("backup-postgres").add_argument("destination")
    sub.add_parser(
        "restore-postgres", help="Stop services; empty initialized PostgreSQL destination only"
    ).add_argument("source")
    child = sub.add_parser("migrate-postgres", help="Offline SQLite to empty PostgreSQL transfer")
    child.add_argument("source")
    child.add_argument("--backup", required=True)
    return p


def run(args):
    command = args.command
    if command == "init":
        return maintenance.init_env(args.env_file)
    local = {
        "config-check",
        "serve",
        "worker",
        "migrate",
        "schema-status",
        "backup",
        "restore",
        "backup-postgres",
        "restore-postgres",
        "migrate-postgres",
    }
    if command in local:
        settings, report = maintenance.configuration()
        if command == "config-check":
            return report
        if not report["valid"]:
            raise maintenance.MaintenanceError(
                "Configuration invalid; run a4g config-check for field guidance"
            )
        if command == "serve":
            import uvicorn
            from .app import create_app

            uvicorn.run(create_app(settings), host=args.host, port=args.port)
            return {"stopped": True}
        if command == "worker":
            from .worker import main

            main(settings)
            return {"stopped": True}
        if command == "migrate":
            return maintenance.migrate(settings, args.backup)
        if command == "restore":
            return maintenance.restore_sqlite(settings, args.source)
        if command in ("schema-status", "backup"):
            if settings.database_url:
                raise maintenance.MaintenanceError(
                    "Use PostgreSQL transfer commands for the configured backend"
                )
            source = settings.data_dir / "agent4good.sqlite3"
            return (
                maintenance.sqlite_info(source)
                if command == "schema-status"
                else maintenance.backup_sqlite(source, args.destination)
            )
        return maintenance.postgres_operation(
            settings,
            command,
            getattr(args, "source", None),
            getattr(args, "destination", None) or getattr(args, "backup", None),
        )
    password_file = args.password_file or os.getenv("A4G_ADMIN_PASSWORD_FILE")
    password = Path(password_file).read_text().strip() if password_file else os.getenv("A4G_ADMIN_PASSWORD")
    if not password:
        password = getpass.getpass("Owner password: ")
    with Client(args.url) as client:
        client.login(password)
        if command == "mission":
            if args.action == "create":
                return client.create_mission(json.loads(Path(args.file).read_text()))
            if args.action == "inspect":
                return client.mission(args.id)
            if args.action == "plan":
                return client.plan_mission(args.id, json.loads(Path(args.file).read_text()))
            if args.action == "review":
                return client.review_mission(args.id, json.loads(Path(args.file).read_text()))
            return client.control_mission(args.id, args.action)
        if command == "debug":
            return client.debug(args.id)
        if command == "replay":
            return client.replay(args.id, offset=args.offset, limit=args.limit)
        if command == "events":
            return client.events(after=args.after, through=args.through, task_id=args.task_id)
        if command == "approvals":
            return client.approvals() if args.action == "list" else client.decide(args.id, args.decision)
        return client.request("GET", "/openapi.json" if command == "schema" else "/" + command)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = run(args)
        print(json.dumps(result, indent=2))
        return 1 if isinstance(result, dict) and result.get("valid") is False else 0
    except (APIError, maintenance.MaintenanceError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
    except httpx.HTTPError:
        print(
            json.dumps(
                {"error": "Connection failed; check URL/TLS and inspect the server before retrying a write"}
            ),
            file=sys.stderr,
        )
    except (OSError, ValueError, RuntimeError):
        print(
            json.dumps(
                {
                    "error": "Operation refused; check input, configuration, file permissions and destination. Existing files are never overwritten."
                }
            ),
            file=sys.stderr,
        )
    except Exception:
        # Final command boundary: provider/storage exceptions can contain credentials.
        print(
            json.dumps({"error": "Operation failed; inspect configuration and protected service logs"}),
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
