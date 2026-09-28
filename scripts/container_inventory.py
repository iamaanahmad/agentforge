"""Inventory a freshly built CI image without running its entrypoint or exporting secrets."""

import argparse
import email
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile
import zipfile


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def notice(path):
    name = PurePosixPath(path).name.lower()
    return bool(re.match(r"^(licen[sc]e|copying|copyright|notice|authors|credits)([.\-_]|$)", name))


def metadata(raw, path):
    msg = email.message_from_bytes(raw)
    return {
        "path": path,
        "name": msg.get("Name"),
        "version": msg.get("Version"),
        "declared_license": msg.get("License-Expression") or msg.get("License"),
        "license_files": msg.get_all("License-File", []),
        "project_urls": msg.get_all("Project-URL", []),
        "source_mapping": "Declared URLs only; corresponding source not verified",
    }


def scan(archive, go_reader=None):
    result = {
        key: []
        for key in (
            "os_packages",
            "python",
            "native_files",
            "notices",
            "wheels",
            "browsers",
            "go",
            "source_files",
        )
    }
    findings = [
        "Inventory is technical evidence, not legal clearance or proof of complete notices.",
        "Native file hashes identify bytes; ABI filenames do not establish upstream versions or source mappings.",
        "Transitive native/Rust source mappings and corresponding-source delivery remain unverified.",
        "Notice paths and hashes show supplied files, not completeness or package-specific legal obligations.",
    ]
    for member in archive:
        path = member.name.removeprefix("./")
        if member.issym() or member.islnk():
            if ".so" in path or notice(path):
                result["native_files" if ".so" in path else "notices"].append(
                    {"path": path, "link_target": member.linkname, "kind": "link"}
                )
            continue
        if not member.isfile():
            continue
        stream = archive.extractfile(member)
        prefix = stream.read(4)
        is_native = prefix == b"\x7fELF"
        is_notice = notice(path)
        is_meta = path.endswith(".dist-info/METADATA") or path.endswith(".egg-info/PKG-INFO")
        is_wheel = path.endswith(".whl")
        is_browser = path.endswith("/playwright/driver/package/browsers.json")
        is_os = path == "var/lib/dpkg/status"
        is_go = path in ("usr/local/bin/minio", "usr/local/bin/mc")
        is_source = path in {
            f"usr/share/licenses/{kind}/{name}" for kind in ("minio", "mc") for name in ("go.mod", "go.sum")
        }
        is_source = is_source or path in {
            f"usr/share/source/{kind}-corresponding-source.tar.gz" for kind in ("minio", "mc")
        }
        if not any((is_native, is_notice, is_meta, is_wheel, is_browser, is_os, is_go, is_source)):
            continue
        digest = hashlib.sha256(prefix)
        # Only metadata and wheel archives need their full bytes in memory.
        data = bytearray(prefix) if any((is_meta, is_wheel, is_browser, is_os, is_go)) else None
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            if data is not None:
                data.extend(chunk)
        entry = {"path": path, "size": member.size, "sha256": digest.hexdigest()}
        if is_native:
            result["native_files"].append(entry)
        if is_notice:
            result["notices"].append(entry)
        if is_source:
            result["source_files"].append(entry)
        if is_meta:
            result["python"].append(metadata(bytes(data), path))
        if is_os:
            for stanza in bytes(data).decode().split("\n\n"):
                msg = email.message_from_string(stanza)
                if msg.get("Status") == "install ok installed":
                    result["os_packages"].append(
                        {k: msg.get(k) for k in ("Package", "Version", "Architecture", "Source")}
                    )
        if is_browser:
            result["browsers"].append({"path": path, "manifest": json.loads(data)})
        if is_wheel:
            wheel = {**entry, "packages": [], "notices": [], "native_files": []}
            with zipfile.ZipFile(io.BytesIO(data)) as contents:
                for name in contents.namelist():
                    if name.endswith(".dist-info/METADATA"):
                        wheel["packages"].append(metadata(contents.read(name), name))
                    if notice(name):
                        wheel["notices"].append(
                            {"path": name, "sha256": hashlib.sha256(contents.read(name)).hexdigest()}
                        )
                    if not name.endswith("/"):
                        with contents.open(name) as inner:
                            if inner.read(4) == b"\x7fELF":
                                wheel["native_files"].append(
                                    {"path": name, "sha256": hashlib.sha256(contents.read(name)).hexdigest()}
                                )
            result["wheels"].append(wheel)
        if is_go:
            build = go_reader(bytes(data)) if go_reader else "UNAVAILABLE: Go reader not supplied"
            result["go"].append({**entry, "build_info": build})
            if build.startswith("UNAVAILABLE"):
                findings.append(f"{path}: {build}")
    if not result["os_packages"]:
        raise ValueError("Expected Debian package database is absent or empty")
    result["findings"] = findings
    return result


def go_info(data):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "binary"
        path.write_bytes(data)
        try:
            info = command("go", "version", "-m", str(path))
            if "\n\t" not in info:
                return "UNAVAILABLE: no embedded Go modules found"
            return info.replace(str(path), "<image-binary>")
        except (OSError, subprocess.CalledProcessError):
            return "UNAVAILABLE: Go build information could not be read"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--kind", required=True, choices=["application", "browser", "sandbox", "minio", "mc"])
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    inspect = json.loads(command("docker", "image", "inspect", args.image))[0]
    # Deliberately exclude Config.Env, image history, and arbitrary labels.
    provenance = {
        "commit": args.commit,
        "image_id": inspect["Id"],
        "repo_digests": inspect.get("RepoDigests", []),
        "platform": inspect["Os"] + "/" + inspect["Architecture"],
        "kind": args.kind,
        "source_labels": {
            k: v
            for k, v in inspect["Config"].get("Labels", {}).items()
            if k
            in (
                "org.opencontainers.image.source",
                "org.opencontainers.image.revision",
                "org.opencontainers.image.licenses",
            )
        }
        if inspect["Config"].get("Labels")
        else {},
        "registry_status": "Local CI build only; not published. Registry digest may be absent.",
    }
    container = command("docker", "create", "--network=none", inspect["Id"])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            export = Path(tmp) / "image.tar"
            subprocess.run(["docker", "export", "--output", str(export), container], check=True)
            with tarfile.open(export) as archive:
                result = scan(archive, go_info)
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=True, stdout=subprocess.DEVNULL)
    result["provenance"] = provenance
    result["runtime_versions"] = []
    if args.kind == "browser":
        # The manifest records expected revisions. Actual paths and hashes are separate evidence.
        installed = [r["path"] for r in result["native_files"] if r["path"].startswith("opt/playwright/")]
        result["installed_browser_files"] = installed
        result["findings"].append(
            "Browser manifest gives Playwright download revisions; source/notice completeness remains unverified."
        )
        for path in installed:
            base = PurePosixPath(path).name
            if base not in ("chrome", "headless_shell", "ffmpeg-linux"):
                continue
            flag = "-version" if base == "ffmpeg-linux" else "--version"
            proc = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network=none",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--pids-limit=64",
                    "--memory=512m",
                    "--entrypoint",
                    "/" + path,
                    inspect["Id"],
                    flag,
                ],
                capture_output=True,
                text=True,
                timeout=45,
            )
            result["runtime_versions"].append(
                {"path": path, "exit_code": proc.returncode, "output": proc.stdout + proc.stderr}
            )
            if proc.returncode:
                result["findings"].append(
                    f"Runtime version unavailable for {path}; see failed version probe."
                )
        if not result["browsers"] or not installed:
            raise ValueError("Browser manifest or downloaded ELF files missing")
        if not result["runtime_versions"]:
            result["findings"].append(
                "Runtime browser versions unavailable: no recognized version probe path."
            )
    if args.kind in ("minio", "mc"):
        if not result["go"]:
            raise ValueError("Storage binary missing")
        supplied = {row["path"] for row in result["notices"]}
        prefix = f"usr/share/licenses/{args.kind}/"
        required_notices = {prefix + name for name in ("LICENSE", "NOTICE", "CREDITS")}
        if missing := required_notices - supplied:
            raise ValueError(f"Storage notices missing: {sorted(missing)}")
        supplied_sources = {row["path"] for row in result["source_files"]}
        required_sources = {prefix + name for name in ("go.mod", "go.sum")}
        if missing := required_sources - supplied_sources:
            raise ValueError(f"Storage source manifests missing: {sorted(missing)}")
    if args.kind in ("application", "browser", "sandbox") and not result["python"]:
        raise ValueError("Python distributions missing")
    if args.kind == "sandbox" and not result["wheels"]:
        raise ValueError("Offline wheelhouse missing")
    if not result["notices"] or not result["native_files"]:
        raise ValueError("Notice or native file evidence missing")
    (args.output / "inventory.json").write_text(json.dumps(result, indent=2) + "\n")
    counts = "\n".join(
        f"- {key}: {len(result[key])}"
        for key in ("os_packages", "python", "native_files", "notices", "wheels")
    )
    (args.output / "summary.md").write_text(
        f"# {args.kind} image evidence\n\nCommit: `{args.commit}`\n\nImage: `{inspect['Id']}`\n\n"
        f"Platform: `{provenance['platform']}`\n\n{counts}\n\n## Limits\n\n"
        + "\n".join("- " + item for item in result["findings"])
        + "\n"
    )


if __name__ == "__main__":
    main()
