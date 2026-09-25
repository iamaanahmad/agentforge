import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from agent4good.app import create_app
from agent4good.config import Settings
from agent4good.branding import load_logo


def test_default_brand_preserves_security(client):
    page = client.get("/")
    assert "<title>agentforge" in page.text
    assert 'data-brand-name="agentforge"' in page.text
    assert "{{BRAND_" not in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert client.get("/api/settings").status_code == 401
    assert client.get("/brand/style.css").headers["content-type"].startswith("text/css")


def test_custom_brand_is_escaped_and_not_recursive(settings):
    settings.brand_name = "<script>alert(1)</script>{{BRAND_LOGO}}"
    settings.brand_tagline = '" onload="alert(1)'
    with TestClient(create_app(settings)) as client:
        page = client.get("/").text
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;alert(1)&lt;/script&gt;{{BRAND_LOGO}}" in page
        assert "&quot; onload=&quot;alert(1)" in page
        assert settings.session_secret not in page
        assert settings.admin_password not in page


@pytest.mark.parametrize(
    "field,value",
    [
        ("brand_name", " "),
        ("brand_name", "x" * 41),
        ("brand_name", "a\nb"),
        ("brand_accent", "#ffffff"),
        ("brand_accent", "red;display:none"),
        ("brand_accent", "#123"),
    ],
)
def test_invalid_brand_fails_startup(settings, field, value):
    values = dict(
        admin_password=settings.admin_password, session_secret=settings.session_secret, secure_cookies=False
    )
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values, **{field: value})


def test_custom_logo_and_color(settings, tmp_path):
    import base64

    logo = tmp_path / "logo.png"
    data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jH1sAAAAASUVORK5CYII="
    )
    logo.write_bytes(data)
    settings.brand_logo_file = logo
    settings.brand_accent = "#263d61"
    with TestClient(create_app(settings)) as client:
        assert 'href="/brand/logo"' in client.get("/").text
        response = client.get("/brand/logo")
        assert response.content == data
        assert response.headers["content-type"] == "image/png"
        assert "#263d61" in client.get("/brand/style.css").text


@pytest.mark.parametrize("data", [b'<svg onload="alert(1)"></svg>', b"<html>secret</html>", b"x" * 262145])
def test_invalid_logo_rejected(tmp_path, data):
    logo = tmp_path / "logo.png"
    logo.write_bytes(data)
    with pytest.raises(ValueError):
        load_logo(logo)
