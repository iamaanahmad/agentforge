# Build the same upstream release from an immutable source revision.
FROM golang:1.25-bookworm AS build
WORKDIR /src
ADD https://github.com/minio/mc/archive/7394ce0dd2a80935aded936b09fa12cbb3cb8096.tar.gz /tmp/source.tar.gz
RUN tar -xzf /tmp/source.tar.gz --strip-components=1 -C /src \
    && CGO_ENABLED=0 go build -trimpath -o /out/mc . \
    && go mod download all \
    && mkdir -p /bundle/upstream /bundle/modules /bundle/go-stdlib \
    && cp -a /src/. /bundle/upstream/ \
    && cp -a /go/pkg/mod/cache/download/. /bundle/modules/ \
    && cp -a /usr/local/go/src/. /bundle/go-stdlib/ \
    && cp /usr/local/go/LICENSE /bundle/go-LICENSE \
    && tar -C /bundle -czf /out/mc-corresponding-source.tar.gz .

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/mc /usr/local/bin/mc
COPY --from=build /out/mc-corresponding-source.tar.gz /usr/share/source/mc-corresponding-source.tar.gz
COPY --from=build /src/LICENSE /usr/share/licenses/mc/LICENSE
COPY --from=build /src/NOTICE /usr/share/licenses/mc/NOTICE
COPY --from=build /src/CREDITS /usr/share/licenses/mc/CREDITS
COPY --from=build /src/go.mod /usr/share/licenses/mc/go.mod
COPY --from=build /src/go.sum /usr/share/licenses/mc/go.sum
LABEL org.opencontainers.image.source="https://github.com/minio/mc" \
      org.opencontainers.image.revision="7394ce0dd2a80935aded936b09fa12cbb3cb8096" \
      org.opencontainers.image.licenses="AGPL-3.0-only"
ENTRYPOINT ["/usr/local/bin/mc"]
