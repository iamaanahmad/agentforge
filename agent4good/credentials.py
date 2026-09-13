"""Host-only encrypted vault and task-scoped brokerage. Never expose secrets through an API/tool."""

import base64
import json
import os
import stat
from contextvars import ContextVar

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .db import now

TASK_CONTEXT = ContextVar("credential_task", default=None)
TOOL_CONTEXT = ContextVar("credential_tool", default=None)
PURPOSES = {
    "openai_api_key": {"model"},
    "anthropic_api_key": {"model"},
    "github_token": {
        "github_read_file",
        "github_list_issues",
        "github_create_issue",
        "github_create_branch",
        "github_write_file",
        "github_open_pr",
        "sandbox_run",
        "github_pr_status",
        "github_merge_pr",
        "github_dispatch_workflow",
        "github_workflow_status",
    },
    "search_api_key": {"web_search"},
    "resend_api_key": {"send_email"},
    "webhook_secret": {"webhook"},
}
PURPOSES["github_app_private_key"] = set(PURPOSES["github_token"])


class CredentialError(RuntimeError):
    pass


def keyring(path, data_dir):
    if path is None:
        raise CredentialError("Configure an external credential key file")
    if path.resolve().is_relative_to(data_dir.resolve()):
        raise CredentialError("Credential keys must be outside the data directory")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
                raise CredentialError("Credential key file requires private owner-only permissions")
            raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError()
            document = json.loads(raw)
        keys = {k: base64.b64decode(v, validate=True) for k, v in document["keys"].items()}
        if not keys or any(len(v) != 32 for v in keys.values()) or document["active"] not in keys:
            raise ValueError()
        return document["active"], keys
    except (OSError, ValueError, KeyError, TypeError):
        raise CredentialError("Credential key file is unavailable or invalid") from None


class CredentialBroker:
    def __init__(self, settings, db):
        self.settings, self.db = settings, db
        self._leased_secrets = set()
        db.bind_security_domain(settings)
        if settings.credential_key_file:
            keyring(settings.credential_key_file, settings.data_dir)
        elif db.one("SELECT 1 FROM credentials LIMIT 1"):
            raise CredentialError("Stored credentials require their external key file")

    def _domain(self):
        row = self.db.one("SELECT * FROM security_domain WHERE id=1")
        if not row or (row["tenant"], row["environment"]) != (
            self.settings.tenant_id,
            self.settings.environment,
        ):
            raise CredentialError("Credential tenant or environment denied")
        return {"tenant": self.settings.tenant_id, "environment": self.settings.environment, "user": "owner"}

    def _decrypt(self, row):
        try:
            _, keys = keyring(self.settings.credential_key_file, self.settings.data_dir)
            aad = json.dumps([row["name"], row["key_id"], row["metadata"]]).encode()
            value = AESGCM(keys[row["key_id"]]).decrypt(row["nonce"], row["ciphertext"], aad).decode()
            metadata = json.loads(row["metadata"])
            if metadata["domain"] != self._domain():
                raise ValueError()
            return value, metadata
        except (KeyError, ValueError, TypeError):
            raise CredentialError("Credential integrity or scope check failed") from None
        except Exception as exc:
            if isinstance(exc, CredentialError):
                raise
            raise CredentialError("Credential integrity or scope check failed") from None

    def put(self, name, value, agents, purposes=None):
        from .catalog import AGENTS

        if name not in PURPOSES or not value or len(value) > 8192:
            raise CredentialError("Invalid credential name or size")
        if name == "webhook_secret" and len(value) < 32:
            raise CredentialError("Webhook secrets require at least 32 characters")
        if not agents or not set(agents) <= {a["id"] for a in AGENTS}:
            raise CredentialError("Choose explicit supported agent roles")
        purposes = set(purposes) if purposes is not None else PURPOSES[name]
        if not purposes or not purposes <= PURPOSES[name]:
            raise CredentialError("Credential purpose is not supported")
        metadata = {"domain": self._domain(), "agents": sorted(set(agents)), "purposes": sorted(purposes)}
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._store(conn, name, value, metadata)
            conn.execute(
                "INSERT INTO events(kind,message,created_at) VALUES (?,?,?)",
                ("credential_changed", name, now()),
            )

    def _store(self, conn, name, value, metadata):
        active, keys = keyring(self.settings.credential_key_file, self.settings.data_dir)
        encoded = json.dumps(metadata, sort_keys=True)
        nonce = os.urandom(12)
        ciphertext = AESGCM(keys[active]).encrypt(
            nonce, value.encode(), json.dumps([name, active, encoded]).encode()
        )
        conn.execute(
            "INSERT OR REPLACE INTO credentials VALUES (?,?,?,?,?)",
            (name, active, nonce, ciphertext, encoded),
        )

    def rotate(self):
        # One transaction: any missing old key leaves every row unchanged.
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in conn.execute("SELECT * FROM credentials").fetchall():
                value, metadata = self._decrypt(row)
                self._store(conn, row["name"], value, metadata)
            conn.execute(
                "INSERT INTO events(kind,message,created_at) VALUES (?,?,?)",
                ("credential_keys_rotated", "Encrypted credentials rewrapped", now()),
            )

    def revoke(self, name):
        if not self.settings.credential_key_file or name not in PURPOSES:
            raise CredentialError("Revocation requires a vault credential name")
        self._domain()
        with self.db.connect() as conn:
            conn.execute("DELETE FROM credentials WHERE name=?", (name,))
            conn.execute(
                "INSERT INTO events(kind,message,created_at) VALUES (?,?,?)",
                ("credential_revoked", name, now()),
            )

    def configured(self, name):
        if name == "github_token" and self.settings.github_app_id and self.settings.github_installation_id:
            return self.configured("github_app_private_key")
        if name not in PURPOSES:
            return bool(getattr(self.settings, name, None))
        if self.db.one("SELECT 1 FROM credentials WHERE name=?", (name,)):
            return True
        # Legacy env-only deployment stays supported, never persisted automatically.
        return not self.settings.credential_key_file and bool(getattr(self.settings, name, ""))

    def get(self, name, purpose, task_id=None):
        self._domain()
        if name not in PURPOSES or purpose not in PURPOSES[name]:
            raise CredentialError("Credential purpose denied")
        task_id = task_id or TASK_CONTEXT.get()
        if purpose == "webhook":
            if task_id:
                raise CredentialError("Worker cannot access webhook credentials")
            task = None
        else:
            task = self.db.task(task_id) if task_id else None
            self.db.require_owner(task)
            if task["status"] != "running":
                raise CredentialError("Credential access requires a running task")
        row = self.db.one("SELECT * FROM credentials WHERE name=?", (name,))
        if row:
            value, metadata = self._decrypt(row)
            from .coordination import ancestors

            parents = []
            if task:
                with self.db.connect() as conn:
                    parents = ancestors(conn, task_id)
            if (
                purpose not in metadata["purposes"]
                or (task and task["agent"] not in metadata["agents"])
                or any(
                    p["agent"] not in metadata["agents"] or p["status"] in {"done", "failed", "cancelled"}
                    for p in parents
                )
            ):
                self.db.event(task_id, "credential_denied", name)
                raise CredentialError("Credential scope denied")
        elif not self.settings.credential_key_file:
            value = getattr(self.settings, name, "")
        else:
            value = ""
        if not value:
            raise CredentialError("Configure the required server credential")
        self._leased_secrets.add(value)
        self.db.event(task_id, "credential_used", name + ":" + purpose)
        return value

    def redact(self, value):
        secrets = [self.settings.admin_password, self.settings.session_secret, *self._leased_secrets]
        secrets.extend(
            getattr(self.settings, name, "")
            for name in (*PURPOSES, "database_url", "s3_access_key", "s3_secret_key")
        )
        if self.settings.credential_key_file:
            secrets.extend(self._decrypt(row)[0] for row in self.db.all("SELECT * FROM credentials"))

        def clean(item):
            if isinstance(item, str):
                for secret in sorted(set(secrets), key=len, reverse=True):
                    if secret:
                        item = item.replace(secret, "[redacted]")
                        item = item.replace(json.dumps(secret)[1:-1], "[redacted]")
                return item
            if isinstance(item, list):
                return [clean(v) for v in item]
            if isinstance(item, dict):
                return {clean(k): clean(v) for k, v in item.items()}
            return item

        return clean(value)
