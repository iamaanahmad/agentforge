import pytest
from fastapi.testclient import TestClient
from agent4good.app import create_app
from agent4good.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        admin_password="test-owner-password-only",
        session_secret="test-session-secret-not-for-deployment-1234",
        secure_cookies=False,
        allowed_hosts=["testserver", "localhost"],
        public_origin="http://testserver",
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


@pytest.fixture
def owner(client, settings):
    response = client.post("/api/login", json={"password": settings.admin_password})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return client
