# Lambda HTTP API v2 adapter

This adapter translates AWS API Gateway HTTP API v2 events into the existing Resembl `http.Handler`, preserving its authorization decision and request context. `cmd/resembl-lambda` builds a Lambda OS-only-runtime bootstrap with the same `DenyAll` authorizer as the local candidate; it permits liveness and refuses queries until an existing owner-backed resource authorization path and engine/index custody are qualified. It does not trust API Gateway identity claims as grants and does not create a Casdoor review API.

Build an artifact without deploying it:

```sh
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o bootstrap ./cmd/resembl-lambda
```

The adapter handles HTTP API v2 only; WebSocket, API Gateway REST API v1 and streaming responses are unsupported. Input is limited to 8 MiB at this boundary and output to 4 MiB before base64 encoding; actual Lambda/API Gateway payload and header limits may be lower. A separately provisioned loopback engine is required; this bootstrap does not create one, guarantee durable work or prove physical stop after cancellation. Lambda integration and native tenant authorization remain unqualified, so this is a deployable deny-by-default transport slice, not a production deployment or a portability parity claim.

The tests exercise the actual handler and a locally simulated Lambda Runtime API, including a built bootstrap, denial of a forged query, malformed event handling, binary output and cancellation propagation. No AWS account or paid service is used.
