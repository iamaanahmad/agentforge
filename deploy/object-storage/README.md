# Optional object storage builds

The former MinIO registry images returned authorization failures during September 25, 2026 release checks. These Dockerfiles build the same server and client release revisions from upstream source. They preserve real storage acceptance tests. The initial build requires network access and can take several minutes.

Server source: https://github.com/minio/minio/tree/07c3a429bfed433e49018cb0f78a52145d4bedeb

Client source: https://github.com/minio/mc/tree/7394ce0dd2a80935aded936b09fa12cbb3cb8096

Both components retain their own AGPL-3.0 licenses. They are separate services, not relicensed by agentforge. The proposed image change copies each pinned source tree's exact `LICENSE`, `NOTICE`, and `CREDITS` files into `/usr/share/licenses/minio/` or `/usr/share/licenses/mc/`. It also copies `go.mod` and `go.sum` to identify the build's linked Go modules. These files come from the same immutable source revision used to build each binary; no agentforge-authored notice text replaces the upstream text.

| Image | Pinned corresponding source | Exact upstream notices |
| --- | --- | --- |
| MinIO server | [Source archive](https://github.com/minio/minio/archive/07c3a429bfed433e49018cb0f78a52145d4bedeb.tar.gz) | [LICENSE](https://raw.githubusercontent.com/minio/minio/07c3a429bfed433e49018cb0f78a52145d4bedeb/LICENSE), [NOTICE](https://raw.githubusercontent.com/minio/minio/07c3a429bfed433e49018cb0f78a52145d4bedeb/NOTICE), [CREDITS](https://raw.githubusercontent.com/minio/minio/07c3a429bfed433e49018cb0f78a52145d4bedeb/CREDITS) |
| MinIO client (`mc`) | [Source archive](https://github.com/minio/mc/archive/7394ce0dd2a80935aded936b09fa12cbb3cb8096.tar.gz) | [LICENSE](https://raw.githubusercontent.com/minio/mc/7394ce0dd2a80935aded936b09fa12cbb3cb8096/LICENSE), [NOTICE](https://raw.githubusercontent.com/minio/mc/7394ce0dd2a80935aded936b09fa12cbb3cb8096/NOTICE), [CREDITS](https://raw.githubusercontent.com/minio/mc/7394ce0dd2a80935aded936b09fa12cbb3cb8096/CREDITS) |

The source archives and notice links returned HTTP 200 during this review. The previous final-image inventories found 241 Go dependency entries in the server binary and 98 in `mc`. Each new build places a corresponding-source archive at `/usr/share/source/<image>-corresponding-source.tar.gz` inside the final image. It contains the pinned upstream tree, downloaded Go module source archives and manifests, and the matching Go standard-library source. Keep this file with any image you distribute. The CI inventory now exports `go-source-map.json`. That file matches each module embedded in the final binary to packaged source bytes, its Go checksum, and any root notice files. A module without a root notice is marked for separate review. The archive also contains modules outside the linked set, so the map is the exact binary subset.

The archive and map provide technical evidence, not legal clearance. The upstream `CREDITS` files give many third-party terms, but each embedded module's terms and any missing or conflicting notices still need legal review. Do not describe these images as cleared for redistribution until that review covers the exact image being shipped. Do not use an older CI artifact as the source route for a later image build.

These pins restore build reproducibility; they do not establish production security or maintenance support. Review upstream maintenance and security before production use. A separately managed S3-compatible endpoint remains supported by the application. The default SQLite installation does not require these containers.
