# Build from the repository root (the COPY paths are relative to it):
#   docker build -f service/Dockerfile --build-arg VERSION=<tag> .
# Passing service/ as the build context instead fails, because the COPY
# instructions below name service/... paths.
# SOURCE_DATE_EPOCH pins the image's created timestamp and the binary's
# embedded build time; TZ=UTC and LC_ALL=C keep the build from depending
# on the host's timezone or locale (reproducible-builds.org).
ARG GO_IMAGE=golang:1.26-alpine@sha256:a2012b4a1d1f9f7e9f44e0d39b805a901e856b1dd80b4e6ee4338db547050308
FROM ${GO_IMAGE} AS build
WORKDIR /src
COPY service/go.mod service/go.sum ./
# Download modules before copying sources, so a source-only edit reuses
# the cached module layer instead of re-fetching every dependency.
RUN go mod download
COPY service/*.go ./
COPY service/cmd ./cmd
COPY service/internal ./internal
ARG SOURCE_DATE_EPOCH=0
ENV SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH} TZ=UTC LC_ALL=C GOFLAGS=-mod=readonly GOTOOLCHAIN=local GOPROXY=off
RUN CGO_ENABLED=0 go build -trimpath -buildvcs=false -ldflags='-s -w' -o /resembl-service ./cmd/resembl-service
FROM scratch
COPY --from=build /resembl-service /resembl-service
# GPL-3.0-only: the license text must accompany the binary in the image,
# not just in the OCI label. NOTICE carries the MIT grant for the vendored
# MinHash/LSH pieces, so both travel with every copy.
COPY LICENSE NOTICE /licenses/
ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="resembl-service" org.opencontainers.image.description="Experimental Go query service for one pinned Resembl index" org.opencontainers.image.source="https://github.com/maci0/resembl" org.opencontainers.image.licenses="GPL-3.0-only" org.opencontainers.image.version="${VERSION}" org.opencontainers.image.revision="${REVISION}"
EXPOSE 8080
# The service exposes GET /healthz (HTTP 204, no dependency I/O) for
# orchestrator probes; use a Kubernetes httpGet probe against /healthz
# instead of a Docker HEALTHCHECK, because the scratch runtime has no
# shell or HTTP client for a CMD to run.
HEALTHCHECK NONE
STOPSIGNAL SIGTERM
USER 65532:65532
ENTRYPOINT ["/resembl-service"]
