"""Evidence must reflect final filesystem bytes, not guessed library versions."""

import hashlib
import io
import tarfile
import zipfile

import pytest

from scripts.container_inventory import scan


def image(files, links=None):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for path, target in (links or {}).items():
            info = tarfile.TarInfo(path)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    buffer.seek(0)
    return tarfile.open(fileobj=buffer)


DPKG = (
    b"Package: libc6\nStatus: install ok installed\nVersion: 2.41-1\nArchitecture: amd64\nSource: glibc\n\n"
)


def test_inventory_preserves_versions_hashes_links_and_wheel_notices():
    wheel = io.BytesIO()
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("demo-1.dist-info/METADATA", "Name: demo\nVersion: 1.2\nLicense-Expression: MIT\n")
        z.writestr("demo-1.dist-info/licenses/LICENSE", "License bytes")
        z.writestr("demo/libfoo.so.3", b"\x7fELFbundled")
    native = b"\x7fELFunknown version"
    files = {
        "var/lib/dpkg/status": DPKG,
        "opt/wheels/demo.whl": wheel.getvalue(),
        "app/.venv/lib/demo.dist-info/METADATA": b"Name: demo\nVersion: 1.2\nLicense-File: LICENSE\n",
        "usr/lib/libfoo.so.3": native,
        "usr/share/doc/foo/copyright": b"notice",
        "usr/local/bin/minio": b"\x7fELFgo",
        "usr/lib/playwright/driver/package/browsers.json": b'{"browsers": [{"name": "chromium", "revision": "1"}]}',
    }
    with image(files, {"usr/lib/libfoo.so": "libfoo.so.3"}) as archive:
        result = scan(archive, lambda data: "embedded module evidence")
    assert result["os_packages"] == [
        {"Package": "libc6", "Version": "2.41-1", "Architecture": "amd64", "Source": "glibc"}
    ]
    assert result["python"][0]["version"] == "1.2"
    library = next(r for r in result["native_files"] if r["path"] == "usr/lib/libfoo.so.3")
    assert library["sha256"] == hashlib.sha256(native).hexdigest()
    assert "version" not in library
    assert result["wheels"][0]["packages"][0]["version"] == "1.2"
    assert len(result["wheels"][0]["notices"]) == 1
    assert len(result["wheels"][0]["native_files"]) == 1
    assert result["go"][0]["build_info"] == "embedded module evidence"
    assert result["browsers"][0]["manifest"]["browsers"][0]["revision"] == "1"


def test_missing_package_database_fails_instead_of_empty_success():
    with image({"unrelated": b"nothing"}) as archive:
        with pytest.raises(ValueError, match="package database"):
            scan(archive)


def test_unavailable_go_evidence_is_explicit():
    with image({"var/lib/dpkg/status": DPKG, "usr/local/bin/mc": b"\x7fELFgo"}) as archive:
        result = scan(archive)
    assert any("UNAVAILABLE" in row for row in result["findings"])
