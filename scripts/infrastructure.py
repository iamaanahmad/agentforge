"""Run with python scripts/infrastructure.py --help. Secrets come from Settings."""

import argparse
import json

from agent4good.config import Settings
from agent4good.postgres import PostgresDatabase
from agent4good.transfer import backup_postgres, migrate_sqlite, restore_postgres


def main():
    parser = argparse.ArgumentParser(description="Private backup and empty-target restoration")
    sub = parser.add_subparsers(dest="command", required=True)
    migrate = sub.add_parser("migrate-sqlite")
    migrate.add_argument("source")
    migrate.add_argument("backup")
    migrate.add_argument("--source-stopped", action="store_true", required=True)
    for name in ("backup", "restore"):
        command = sub.add_parser(name)
        command.add_argument("path")
    sub.add_parser("status")
    args = parser.parse_args()
    settings = Settings()
    if not settings.database_url:
        parser.error("Configure A4G_DATABASE_URL for the target PostgreSQL database")
    db = PostgresDatabase(settings)
    if args.command == "migrate-sqlite":
        result = migrate_sqlite(args.source, args.backup, db)
    elif args.command == "backup":
        result = backup_postgres(db, args.path)
    elif args.command == "restore":
        result = restore_postgres(db, args.path)
    else:
        result = db.metrics()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(
            "Infrastructure operation failed; check source, empty target, storage access, and schema version"
        ) from None
