# The component graph matches the existing Collector release; no SDK/sidecar.
FROM golang:1.24.9 AS build
ENV GOMAXPROCS=2 GOFLAGS=-p=2 CGO_ENABLED=0 GOTOOLCHAIN=local GOMEMLIMIT=768MiB
WORKDIR /build
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    go install go.opentelemetry.io/collector/cmd/builder@v0.137.0
COPY linkprocessor ./linkprocessor
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    cd linkprocessor && go test -mod=mod ./...
COPY builder-config.yaml ./builder-config.yaml
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    /go/bin/builder --config=/build/builder-config.yaml --skip-strict-versioning=false --verbose
FROM otel/opentelemetry-collector-contrib:0.137.0
COPY --from=build /build/dist/otelcol-contrib /otelcol-contrib
