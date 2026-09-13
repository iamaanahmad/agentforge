"""Restricted Dockerfile packager. Run inside the sandbox. No daemon, sockets, RUN, ADD, or downloads."""

import hashlib
import io
import json
from pathlib import Path
import shlex
import tarfile
import sys


def build(dockerfile, output):
    root = Path.cwd().resolve()
    config = {"architecture": "amd64", "os": "linux", "config": {"User": "10001:10001"}}
    layer = io.BytesIO()
    started = False
    with tarfile.open(fileobj=layer, mode="w") as tar:
        for line in Path(dockerfile).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            op, _, args = line.partition(" ")
            if not started:
                if line != "FROM scratch":
                    raise ValueError("Only FROM scratch is supported")
                started = True
            elif op == "COPY":
                src, dest = shlex.split(args)
                path = (root / src).resolve()
                if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 10000000:
                    raise ValueError("COPY requires a bounded workspace file")
                if not dest.startswith("/") or ".." in dest.split("/"):
                    raise ValueError("COPY destination must be absolute and cannot traverse")
                data = path.read_bytes()
                info = tarfile.TarInfo(dest.lstrip("/"))
                info.size, info.mode = len(data), 0o755 if path.stat().st_mode & 0o111 else 0o644
                tar.addfile(info, io.BytesIO(data))
            elif op in {"CMD", "ENTRYPOINT"}:
                value = json.loads(args)
                if not isinstance(value, list) or not value or any(not isinstance(v, str) for v in value):
                    raise ValueError("Use JSON exec syntax")
                config["config"]["Cmd" if op == "CMD" else "Entrypoint"] = value
            else:
                raise ValueError("Unsupported Dockerfile instruction; build code in the sandbox first")
    if not started:
        raise ValueError("Missing FROM scratch")
    data = layer.getvalue()
    config["rootfs"] = {"type": "layers", "diff_ids": ["sha256:" + hashlib.sha256(data).hexdigest()]}
    encoded = json.dumps(config).encode()
    config_name = hashlib.sha256(encoded).hexdigest() + ".json"
    manifest = [{"Config": config_name, "RepoTags": ["agent4good-sandbox:artifact"], "Layers": ["layer.tar"]}]
    with tarfile.open(output, "w") as tar:
        for name, content in [
            (config_name, encoded),
            ("layer.tar", data),
            ("manifest.json", json.dumps(manifest).encode()),
        ]:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))


if __name__ == "__main__":
    build(*sys.argv[1:])
