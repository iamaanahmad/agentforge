"""Match embedded Go modules to the source and notice bytes shipped with an image."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import zipfile


NOTICE_NAME = re.compile(
    r"^(unlicense|license|licence|copying|notice|copyright|credits|authors)([._-]|$)", re.I
)
GO_LICENSE_REFERENCE = re.compile(r"license that can be found in (?:the )?https://golang\.org/LICENSE", re.I)


def escape_go(value):
    return "".join("!" + char.lower() if char.isupper() else char for char in value)


def embedded_modules(build_info):
    modules = []
    for line in build_info.splitlines():
        fields = line.strip().split("\t")
        if len(fields) >= 4 and fields[0] == "dep":
            modules.append({"module": fields[1], "version": fields[2], "go_hash": fields[3]})
        elif fields and fields[0] == "=>":
            raise ValueError("Replaced module needs explicit source review")
    if not modules:
        raise ValueError("No embedded Go dependencies found")
    return modules


def member_bytes(archive, name):
    try:
        member = archive.getmember("./" + name)
    except KeyError as exc:
        raise ValueError(f"Source package misses {name}") from exc
    return archive.extractfile(member).read()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def audit(inventory, source_archive):
    modules = embedded_modules(inventory["go"][0]["build_info"])
    rows = []
    with tarfile.open(source_archive, "r:gz") as archive:
        member_bytes(archive, "upstream/LICENSE")
        member_bytes(archive, "upstream/go.mod")
        member_bytes(archive, "go-LICENSE")
        if not any(m.name.startswith("./go-stdlib/") and m.isfile() for m in archive.getmembers()):
            raise ValueError("Go standard-library source missing")
        for item in modules:
            prefix = f"modules/{escape_go(item['module'])}/@v/{escape_go(item['version'])}"
            zip_name = prefix + ".zip"
            source = member_bytes(archive, zip_name)
            cache_hash = member_bytes(archive, prefix + ".ziphash").decode().strip()
            if cache_hash != item["go_hash"]:
                raise ValueError(f"Go checksum differs for {item['module']}@{item['version']}")
            member_bytes(archive, prefix + ".mod")
            with zipfile.ZipFile(io.BytesIO(source)) as module_zip:
                expected_root = f"{item['module']}@{item['version']}/"
                if not any(name.startswith(expected_root) for name in module_zip.namelist()):
                    expected_root = f"{escape_go(item['module'])}@{escape_go(item['version'])}/"
                if not all(name.startswith(expected_root) for name in module_zip.namelist()):
                    raise ValueError(f"Unexpected source paths for {item['module']}@{item['version']}")
                notices = []
                source_license_references = []
                for name in module_zip.namelist():
                    relative = name[len(expected_root) :]
                    if "/" not in relative and NOTICE_NAME.match(relative):
                        notices.append(
                            {"path": name, "sha256": hashlib.sha256(module_zip.read(name)).hexdigest()}
                        )
                if not notices:
                    for name in module_zip.namelist():
                        if not name.endswith(".go"):
                            continue
                        source_file = module_zip.read(name)
                        if GO_LICENSE_REFERENCE.search(source_file.decode("utf-8", errors="replace")):
                            source_license_references.append(
                                {
                                    "path": name,
                                    "sha256": hashlib.sha256(source_file).hexdigest(),
                                    "referenced_license_path": "go-LICENSE",
                                }
                            )
            rows.append(
                {
                    **item,
                    "source_path": zip_name,
                    "source_sha256": hashlib.sha256(source).hexdigest(),
                    "notice_files": notices,
                    "source_license_references": source_license_references,
                    "notice_status": "root notice found"
                    if notices
                    else "no root notice; source headers may reference Go license; review upstream terms",
                }
            )
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", type=Path)
    parser.add_argument("source_archive", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    inventory = json.loads(args.inventory.read_text())
    rows = audit(inventory, args.source_archive)
    args.output.write_text(
        json.dumps(
            {
                "image": inventory["provenance"],
                "binary_sha256": inventory["go"][0]["sha256"],
                "source_archive_sha256": file_sha256(args.source_archive),
                "modules": rows,
                "missing_root_notices": [
                    f"{row['module']}@{row['version']}" for row in rows if not row["notice_files"]
                ],
                "limit": "Files and checksums are evidence. License compatibility and completeness need legal review.",
            },
            indent=2,
        )
        + "\n"
    )
    print(f"Mapped {len(rows)} embedded modules to packaged source and notice records")
    print(f"Modules without a root notice: {sum(not row['notice_files'] for row in rows)}")


if __name__ == "__main__":
    main()
