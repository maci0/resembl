# Build from the repository root (the COPY paths are relative to it):
#   docker build --platform linux/amd64 -f service/Dockerfile --build-arg VERSION=<tag> .
# Passing service/ as the build context instead fails, because the COPY
# instructions below name service/... paths.
# Multi-arch: `docker buildx build --platform linux/arm64` builds an arm64
# image because the RUN below pins GOARCH to TARGETARCH (BuildKit's
# predefined per-stage architecture), so the binary always matches the
# image. Without that pin a `docker buildx build --platform linux/arm64`
# on an amd64 builder would silently produce an amd64 binary in an arm64
# image that fails with "exec format error" on the target.
# SOURCE_DATE_EPOCH pins the image's created timestamp and the binary's
# embedded build time; TZ=UTC and LC_ALL=C keep the build from depending
# on the host's timezone or locale (reproducible-builds.org).
ARG GO_IMAGE=golang:1.27-alpine@sha256:eb61c3873c97416fff0116553fff65d67a3549f72e11d9758849a689cdd8b523
FROM ${GO_IMAGE} AS build
WORKDIR /src
COPY service/go.mod service/go.sum ./
# Download modules before copying sources, so a source-only edit reuses
# the cached module layer instead of re-fetching every dependency.
RUN go mod download
COPY LICENSE NOTICE /tmp/licenses-src/
COPY service/*.go ./
COPY service/cmd ./cmd
COPY service/internal ./internal
ARG SOURCE_DATE_EPOCH=0
ENV SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH} TZ=UTC LC_ALL=C GOFLAGS=-mod=readonly GOTOOLCHAIN=local GOPROXY=off
# TARGETARCH is BuildKit's predefined arg for the stage's architecture
# (linux/amd64 -> amd64, linux/arm64 -> arm64). Pinning it makes the
# Dockerfile build the correct arch for `docker buildx build --platform`
# instead of relying on the builder host's native arch: the prior
# unpinned build silently embedded an amd64 binary in an arm64 image,
# producing "exec format error" on the target while looking green at
# build time. If TARGETARCH is unset (a plain `docker build` on an old
# BuildKit), fall back to the builder's own GOARCH.
ARG TARGETARCH
RUN TARGETARCH="${TARGETARCH:-$(go env GOARCH)}" && CGO_ENABLED=0 GOOS=linux GOARCH="$TARGETARCH" go build -trimpath -buildvcs=false -ldflags='-s -w' -o /resembl-service ./cmd/resembl-service && mkdir -p /tmp/lic-out/licenses && cp /tmp/licenses-src/LICENSE /tmp/licenses-src/NOTICE /tmp/lic-out/licenses/ && touch -t $(date -u -d "@${SOURCE_DATE_EPOCH}" +%Y%m%d%H%M.%S) /resembl-service /tmp/lic-out/licenses/LICENSE /tmp/lic-out/licenses/NOTICE /tmp/lic-out/licenses
FROM scratch
COPY --from=build /resembl-service /resembl-service
# GPL-3.0-only: the license text must accompany the binary in the image,
# not just in the OCI label. NOTICE carries the MIT grant for the vendored
# MinHash/LSH pieces, so both travel with every copy.
COPY --from=build /tmp/lic-out/licenses /licenses
ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="resembl-service" org.opencontainers.image.description="Experimental Go query service for one pinned Resembl index" org.opencontainers.image.source="https://github.com/maci0/resembl" org.opencontainers.image.url="https://github.com/maci0/resembl" org.opencontainers.image.licenses="GPL-3.0-only" org.opencontainers.image.version="${VERSION}" org.opencontainers.image.revision="${REVISION}"
EXPOSE 8080
# The service exposes GET /healthz (HTTP 204, no dependency I/O) for
# orchestrator probes; use a Kubernetes httpGet probe against /healthz
# instead of a Docker HEALTHCHECK, because the scratch runtime has no
# shell or HTTP client for a CMD to run.
HEALTHCHECK NONE
STOPSIGNAL SIGTERM
USER 65532:65532
ENTRYPOINT ["/resembl-service"]
