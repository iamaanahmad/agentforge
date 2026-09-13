"""Host operator CLI. Values enter through hidden input/stdin, never arguments or output."""

import argparse
import base64
import json
import os
import secrets
import sys
from getpass import getpass
from pathlib import Path

from .config import Settings
from .credentials import CredentialBroker, CredentialError, PURPOSES, keyring
from .db import Database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-key")
    init.add_argument("path", type=Path)
    put = commands.add_parser("put")
    put.add_argument("name", choices=PURPOSES)
    put.add_argument("--agent", action="append", required=True)
    put.add_argument("--purpose", action="append")
    put.add_argument("--stdin", action="store_true")
    revoke = commands.add_parser("revoke")
    revoke.add_argument("name", choices=PURPOSES)
    commands.add_parser("rotate")
    commands.add_parser("rewrap")
    commands.add_parser("status")
    args = parser.parse_args()
    settings = Settings()
    if args.command == "init-key":
        if args.path.resolve().is_relative_to(settings.data_dir.resolve()):
            raise CredentialError("Key must be outside the data directory")
        document = {"active": "k_" + secrets.token_hex(8), "keys": {}}
        document["keys"][document["active"]] = base64.b64encode(secrets.token_bytes(32)).decode()
        fd = os.open(args.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(document, stream)
            stream.flush()
            os.fsync(stream.fileno())
        print("Created a private key file. Configure A4G_CREDENTIAL_KEY_FILE to use it.")
        return
    db = Database.from_settings(settings)
    broker = CredentialBroker(settings, db)
    if args.command == "put":
        value = sys.stdin.read(8194).rstrip("\n") if args.stdin else getpass("Credential value: ")
        broker.put(args.name, value, args.agent, args.purpose)
        print("Encrypted credential saved. Remove its legacy environment value and restart both processes.")
    elif args.command == "revoke":
        broker.revoke(args.name)
        print("Credential revoked for future dispatches. In-flight requests may finish.")
    elif args.command == "rotate":
        # Stop both services first. Retain old keys for backups and crash recovery.
        active, keys = keyring(settings.credential_key_file, settings.data_dir)
        active = "k_" + secrets.token_hex(8)
        keys[active] = secrets.token_bytes(32)
        document = {"active": active, "keys": {k: base64.b64encode(v).decode() for k, v in keys.items()}}
        path = settings.credential_key_file
        temporary = path.with_name(path.name + "." + secrets.token_hex(8))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(document, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        broker.rotate()
        print("Credentials rewrapped. Back up the keyring separately; retain old keys for older backups.")
    elif args.command == "rewrap":
        broker.rotate()
        print("Credentials rewrapped with the active key.")
    else:
        print(
            json.dumps(
                {
                    "mode": "encrypted-vault" if settings.credential_key_file else "legacy-environment",
                    "credentials": [r["name"] for r in db.all("SELECT name FROM credentials ORDER BY name")],
                }
            )
        )


if __name__ == "__main__":
    try:
        main()
    except (CredentialError, OSError, ValueError):
        raise SystemExit(
            "Vault operation refused. Check private key configuration, scope, and input."
        ) from None
