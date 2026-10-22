# Build the same upstream release from an immutable source revision.
FROM golang:1.25-bookworm AS build
WORKDIR /src
ADD https://github.com/minio/minio/archive/07c3a429bfed433e49018cb0f78a52145d4bedeb.tar.gz /tmp/source.tar.gz
RUN tar -xzf /tmp/source.tar.gz --strip-components=1 -C /src \
    && CGO_ENABLED=0 go build -trimpath -o /out/minio . \
    && go mod download all \
    && mkdir -p /bundle/upstream /bundle/modules /bundle/go-stdlib \
    && cp -a /src/. /bundle/upstream/ \
    && cp -a /go/pkg/mod/cache/download/. /bundle/modules/ \
    && cp -a /usr/local/go/src/. /bundle/go-stdlib/ \
    && cp /usr/local/go/LICENSE /bundle/go-LICENSE \
    && tar -C /bundle -czf /out/minio-corresponding-source.tar.gz .

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/minio /usr/local/bin/minio
COPY --from=build /out/minio-corresponding-source.tar.gz /usr/share/source/minio-corresponding-source.tar.gz
COPY --from=build /src/LICENSE /usr/share/licenses/minio/LICENSE
COPY --from=build /src/NOTICE /usr/share/licenses/minio/NOTICE
COPY --from=build /src/CREDITS /usr/share/licenses/minio/CREDITS
COPY --from=build /src/go.mod /usr/share/licenses/minio/go.mod
COPY --from=build /src/go.sum /usr/share/licenses/minio/go.sum
LABEL org.opencontainers.image.source="https://github.com/minio/minio" \
      org.opencontainers.image.revision="07c3a429bfed433e49018cb0f78a52145d4bedeb" \
      org.opencontainers.image.licenses="AGPL-3.0-only"
COPY --from=build /src/dockerscripts/docker-entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
