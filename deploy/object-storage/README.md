# Optional object storage builds

The former MinIO registry images returned authorization failures during September 25, 2026 release checks. These Dockerfiles build the same server and client release revisions from upstream source. They preserve real storage acceptance tests. The initial build requires network access and can take several minutes.

Server source: https://github.com/minio/minio/tree/07c3a429bfed433e49018cb0f78a52145d4bedeb

Client source: https://github.com/minio/mc/tree/7394ce0dd2a80935aded936b09fa12cbb3cb8096

Both components retain their own AGPL-3.0 licenses, copied into each image. They are separate services, not relicensed by agentforge. Keep these notices and corresponding upstream source available when distributing these images. Assess your distribution and modification obligations before redistribution. The project license decision does not remove third-party obligations.

These pins restore build reproducibility; they do not establish production security or maintenance support. Review upstream maintenance and security before production use. A separately managed S3-compatible endpoint remains supported by the application. The default SQLite installation does not require these containers.
