import pytest

from agent4good.config import Settings
from agent4good.credentials import CredentialBroker
from agent4good.db import Database


def test_mounted_secrets_and_infrastructure_redaction(settings, tmp_path, monkeypatch):
    secret = tmp_path / "database_url"
    secret.write_text("postgresql://internal:private-password@postgres/app")
    monkeypatch.setenv("A4G_DATABASE_URL_FILE", str(secret))
    mounted = Settings(
        _env_file=None,
        admin_password=settings.admin_password,
        session_secret=settings.session_secret,
        secure_cookies=False,
        s3_bucket="app",
    )
    assert mounted.database_url == secret.read_text()
    broker = CredentialBroker(mounted, Database(tmp_path / "test.sqlite3"))
    assert broker.redact("Error " + mounted.database_url) == "Error [redacted]"
    assert mounted.database_url not in repr(mounted)
    assert "database_url" not in mounted.model_dump()
    secret.unlink()
    with pytest.raises(ValueError, match="secret file"):
        Settings(_env_file=None)


def test_postgres_requires_object_bucket(settings):
    with pytest.raises(ValueError, match="object storage bucket"):
        Settings(
            _env_file=None,
            admin_password=settings.admin_password,
            session_secret=settings.session_secret,
            secure_cookies=False,
            database_url="postgresql://localhost/app",
        )
