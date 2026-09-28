"""Source maps must follow the binary's module hashes and bundled bytes."""

import io
import tarfile
import zipfile

import pytest

from scripts.audit_go_sources import audit, escape_go


def fixture(tmp_path, cache_hash="h1:correct"):
    module = io.BytesIO()
    with zipfile.ZipFile(module, "w") as archive:
        archive.writestr("example.com/Widget@v1.0.0/LICENSE", "MIT license text")
        archive.writestr("example.com/Widget@v1.0.0/main.go", "package widget")
    source = tmp_path / "source.tar.gz"
    prefix = "modules/example.com/!widget/@v/v1.0.0"
    files = {
        "upstream/LICENSE": b"AGPL license text",
        "upstream/go.mod": b"module example.com/main\n",
        "go-LICENSE": b"Go license text",
        "go-stdlib/fmt/print.go": b"package fmt",
        prefix + ".zip": module.getvalue(),
        prefix + ".ziphash": cache_hash.encode(),
        prefix + ".mod": b"module example.com/Widget\n",
    }
    with tarfile.open(source, "w:gz") as archive:
        for name, data in files.items():
            entry = tarfile.TarInfo("./" + name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    inventory = {"go": [{"build_info": "binary: go1.25\n\tdep\texample.com/Widget\tv1.0.0\th1:correct"}]}
    return source, inventory


def test_source_map_requires_exact_module_and_notice_bytes(tmp_path):
    source, inventory = fixture(tmp_path)
    rows = audit(inventory, source)
    assert len(rows) == 1
    assert rows[0]["source_path"] == "modules/example.com/!widget/@v/v1.0.0.zip"
    assert rows[0]["notice_files"][0]["path"].endswith("/LICENSE")
    assert escape_go("Example.com/Widget") == "!example.com/!widget"


def test_source_map_rejects_checksum_mismatch(tmp_path):
    source, inventory = fixture(tmp_path, "h1:wrong")
    with pytest.raises(ValueError, match="checksum differs"):
        audit(inventory, source)
