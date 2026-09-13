"""Private text objects with database references and content integrity checks."""

import hashlib

import boto3
from botocore.config import Config

from .db import now


class ObjectStore:
    def __init__(self, settings):
        self.bucket = settings.s3_bucket
        self.prefix = f"{settings.tenant_id}/{settings.environment}/artifacts/"
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint or None,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key or None,
            aws_secret_access_key=settings.s3_secret_key or None,
            config=Config(
                connect_timeout=5,
                read_timeout=15,
                retries={"max_attempts": 2},
                s3={"addressing_style": "path"},
            ),
        )

    def health(self):
        self.client.head_bucket(Bucket=self.bucket)

    def put(self, artifact_id, content):
        body = content.encode()
        digest = hashlib.sha256(body).hexdigest()
        key = self.prefix + artifact_id + "/" + digest
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="text/plain")
        # Do not publish the reference until a read verifies storage acceptance.
        if self.read(key, digest) != content:
            raise RuntimeError("Object storage verification failed")
        return key, digest

    def read(self, key, digest):
        if not key.startswith(self.prefix):
            raise RuntimeError("Artifact belongs to another storage domain")
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        with response["Body"] as stream:
            body = stream.read(1000001)
        if len(body) > 1000000 or hashlib.sha256(body).hexdigest() != digest:
            raise RuntimeError("Artifact integrity check failed")
        return body.decode()


def save_artifact(db, settings, artifact_id, task_id, name, content):
    obj = ObjectStore(settings).put(artifact_id, content) if db.distributed else None
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO artifacts VALUES (?,?,?,?,?)",
            (artifact_id, task_id, name, "" if obj else content, now()),
        )
        if obj:
            conn.execute("INSERT INTO artifact_objects VALUES (?,?,?)", (artifact_id, *obj))


def read_artifact(db, settings, row):
    if db.distributed:
        obj = db.one("SELECT * FROM artifact_objects WHERE artifact_id=?", (row["id"],))
        if obj:
            return ObjectStore(settings).read(obj["object_key"], obj["sha256"])
    # Migrated historical inline text remains readable until explicitly exported.
    return row["content"]
