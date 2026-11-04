ARG GO_IMAGE=golang:1.26-alpine@sha256:a2012b4a1d1f9f7e9f44e0d39b805a901e856b1dd80b4e6ee4338db547050308
FROM ${GO_IMAGE} AS build
WORKDIR /src
COPY service/go.mod service/go.sum ./
COPY service/*.go ./
COPY service/cmd ./cmd
COPY service/internal ./internal
RUN CGO_ENABLED=0 go build -trimpath -buildvcs=false -ldflags='-s -w' -o /resembl-service ./cmd/resembl-service
FROM scratch
COPY --from=build /resembl-service /resembl-service
LABEL org.opencontainers.image.title="resembl-service" org.opencontainers.image.description="Experimental Go query service for one pinned Resembl index" org.opencontainers.image.source="https://github.com/maci0/resembl" org.opencontainers.image.licenses="GPL-3.0-only"
USER 65532:65532
ENTRYPOINT ["/resembl-service"]
