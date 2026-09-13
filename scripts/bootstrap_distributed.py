"""Generate fresh private mounts. Never overwrite secrets or print their contents."""

import argparse
import base64
import json
import os
from pathlib import Path
import secrets


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path, nargs="?", default=Path(".infra-secrets"))
    args = parser.parse_args()
    directory = args.directory.resolve()
    directory.mkdir(mode=0o700)  # Refuse existing directories, including existing installations.
    pg = secrets.token_urlsafe(32)
    values = {
        "postgres_password": pg,
        "database_url": f"postgresql://agent4good:{pg}@postgres:5432/agent4good",
        "admin_password": secrets.token_urlsafe(32),
        "session_secret": secrets.token_urlsafe(48),
        "object_root_access": secrets.token_hex(12),
        "object_root_secret": secrets.token_urlsafe(32),
        "object_access": secrets.token_hex(12),
        "object_secret": secrets.token_urlsafe(32),
    }
    for name, value in values.items():
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
    keys = directory / "keys"
    keys.mkdir(mode=0o700)
    fd = os.open(keys / "keyring.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(
            {"active": "initial", "keys": {"initial": base64.b64encode(secrets.token_bytes(32)).decode()}},
            stream,
        )
    print(
        "Private secret mounts created. Set keys directory ownership to container UID 10001 before starting."
    )


if __name__ == "__main__":
    main()
