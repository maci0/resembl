ARG GO_IMAGE=golang:1.26-alpine@sha256:a2012b4a1d1f9f7e9f44e0d39b805a901e856b1dd80b4e6ee4338db547050308
FROM ${GO_IMAGE} AS build
WORKDIR /src
COPY service/go.mod ./
COPY service/*.go ./
COPY service/cmd ./cmd
RUN CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o /resembl-service ./cmd/resembl-service
FROM scratch
COPY --from=build /resembl-service /resembl-service
USER 65532:65532
ENTRYPOINT ["/resembl-service"]
