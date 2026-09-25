# Dependency license review

Checked September 25, 2026 against the frozen Python environment, including development tools. This inventory reports package metadata, not legal clearance. Recheck the actual artifacts when shipping wheels or containers. No project LICENSE has been adopted.

## Python inventory

| Package | Version | Declared license |
|---|---|---|
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| attrs | 26.1.0 | MIT |
| boto3 | 1.43.93 | Apache-2.0 |
| botocore | 1.43.93 | Apache-2.0 |
| certifi | 2026.7.22 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| charset-normalizer | 3.5.1 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| cryptography | 46.0.7 | Apache-2.0 OR BSD-3-Clause |
| fastapi | 0.141.1 | MIT |
| google-auth | 2.58.0 | Apache 2.0 |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| idna | 3.19 | BSD-3-Clause |
| iniconfig | 2.3.0 | MIT |
| jmespath | 1.1.0 | MIT |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pluggy | 1.6.0 | MIT |
| psycopg | 3.3.5 | LGPL-3.0-only |
| psycopg-binary | 3.3.5 | LGPL-3.0-only |
| pyasn1 | 0.6.4 | BSD-2-Clause |
| pyasn1_modules | 0.4.2 | BSD |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.13.5 | MIT |
| pydantic-settings | 2.15.0 | MIT |
| pydantic_core | 2.46.5 | MIT |
| Pygments | 2.21.0 | BSD-2-Clause |
| pytest | 9.1.1 | MIT |
| python-dateutil | 2.9.0.post0 | Dual License |
| python-dotenv | 1.2.3 | BSD-3-Clause |
| referencing | 0.37.0 | MIT |
| requests | 2.34.2 | Apache-2.0 |
| rpds-py | 2026.6.3 | MIT |
| ruff | 0.16.7 | MIT |
| s3transfer | 0.19.2 | Apache License 2.0 |
| six | 1.17.0 | MIT |
| starlette | 1.6.0 | BSD-3-Clause |
| typing-inspection | 0.4.4 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| urllib3 | 2.7.0 | MIT |
| uvicorn | 0.52.4 | BSD-3-Clause |

## Obligations to preserve

- Retain upstream copyright and license files when redistributing dependencies. Keep Apache NOTICE files where provided.
- Psycopg and psycopg-binary declare LGPL-3.0-only. Keep their license, corresponding-source route, and replacement/relinking rights when distributing binaries. Do not represent them as MIT code.
- Certifi declares MPL-2.0. Keep notices and source availability for covered files, including any changes.
- Binary wheels include native libraries. Audit their bundled license directories and corresponding source before publishing a binary release. Python metadata alone does not cover these files.

## Optional containers

The distributed example builds separate MinIO server and client services from pinned upstream source. Both use AGPL-3.0; their license files are copied into the images. See [the source pins and notices](../deploy/object-storage/README.md). Keep corresponding source available and review network-use obligations for modified versions.

PostgreSQL, Caddy, Python, Go, Debian system packages, Chromium and the browser broker dependencies keep their separate terms. These are downloaded during builds, not vendored into this source tree. Container redistribution requires a per-image bill of materials and notices. This review does not clear a bundled container release.

## Release conclusion

No Python metadata entry is unknown in this checked environment. Permissive licenses dominate, but LGPL, MPL and AGPL obligations remain. No finding here requires hiding the project's own source. Publishing source does not itself grant a license, and a project license cannot replace third-party terms.

The blocking project decision is adoption of a project license. A binary/container release also requires the artifact-level notice and source review above. Do not claim all components have one permissive license.

Sources: installed `.dist-info/METADATA` and license directories, `uv.lock`, deployment Dockerfiles, [Psycopg source license](https://github.com/psycopg/psycopg/blob/master/LICENSE.txt), [Certifi license](https://github.com/certifi/python-certifi/blob/master/LICENSE), and [MinIO source](https://github.com/minio/minio).
