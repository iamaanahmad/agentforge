# Build the same upstream release from an immutable source revision.
FROM golang:1.25-bookworm AS build
WORKDIR /src
ADD https://github.com/minio/mc/archive/7394ce0dd2a80935aded936b09fa12cbb3cb8096.tar.gz /tmp/source.tar.gz
RUN tar -xzf /tmp/source.tar.gz --strip-components=1 -C /src \
    && CGO_ENABLED=0 go build -trimpath -o /out/mc .

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/mc /usr/local/bin/mc
COPY --from=build /src/LICENSE /usr/share/licenses/mc/LICENSE
LABEL org.opencontainers.image.source="https://github.com/minio/mc" \
      org.opencontainers.image.revision="7394ce0dd2a80935aded936b09fa12cbb3cb8096" \
      org.opencontainers.image.licenses="AGPL-3.0-only"
ENTRYPOINT ["/usr/local/bin/mc"]
