# Resembl Go service candidate

This package is an experimental portable HTTP boundary for one preconfigured Resembl index. It does not replace the embedded Python API, CLI or local `resembl serve`. It is not a deployable public service: an application must provide an `Authorizer` that authenticates the caller and checks the exact configured resource and `query` verb against current native authority. Missing authority is rejected at construction. No Casdoor authorization-review endpoint is assumed to exist.

`POST /v1/find` accepts the existing UTF-8 `/find` JSON body (maximum 8 MiB), requires a nonempty query, and forwards it through the `Engine` port after authorization. Concurrency and request timeout are configured at construction. `GET /healthz` is local process liveness and performs no dependency I/O. Unavailable authority or engine fails closed. Engine results must be valid UTF-8 JSON. Results must be treated as sensitive corpus data; responses are not cacheable.

`NewLocalEngine` is an optional adapter to a separately started `resembl serve` on loopback. The host must isolate that unauthenticated backend, pin and verify its engine version, configuration and index identity, and provision its resource limits. The adapter refuses redirects and bounds response size. Truncated and oversized error bodies are transport failures, not completed rejections. The portable handler itself does not launch processes and can use a different qualified backend on VM, container, Kubernetes or serverless environments. A caller timeout does not prove that the Python engine stopped computing. The VM/container command requires a private, pre-created persistent custody directory. It atomically reserves a slot before dispatch and leaves it occupied after an uncertain backend outcome, including across process restart. Fully received validation rejections (400 and 413) release their slot; all other engine errors retain it. All replicas sharing an engine must share the same persistent directory and capacity setting. This single-host filesystem adapter is not a distributed or serverless custody owner; it does not verify physical stop or automatically release orphaned claims. A qualified operator must establish that the exact engine execution stopped before repairing any slot. Do not use this as a durable cancellable job or for mutations.

The candidate also forwards `/v1/find-batch` with the existing engine batch semantics. `cmd/resembl-service` and its container entry point default to deny-all authorization. For a private single-index service, `-token-file` can enable an operator-managed bearer credential for the configured resource. Generate 32 random bytes and encode them as 64 lowercase hex characters; place the value in a private regular file readable only by the service. It is read once at startup: restart the service to rotate or revoke it. This grants all holders query access to that index and is not a native tenant identity, per-user authorization, or audit trail. Use a trusted authenticated TLS proxy and network controls when clients cross a host boundary; never pass the secret in command-line arguments. Supply `-custody` pointing to the private persistent directory; startup fails without it. The local engine and its index must be provisioned separately. This slice does not yet expose index mutation, durable jobs, real Casdoor integration or version/index attestation. Those need an owner-backed protocol, native authorization and compatibility qualification before public rollout. The existing Python interfaces remain unchanged.

Run `go test -race ./...` here. To exercise the real local engine, start a disposable `resembl serve` and set `RESEMBL_TEST_ENGINE_URL` to its loopback URL for `go test -run TestActualWarmEngine`.

The experimental `PinnedSQLiteEngine` calls `/find-pinned` on a compatible warm engine. That endpoint takes a canonical SHA-256 over the actual SQLite snippet, LSH and metadata rows while holding a SQLite writer fence, checks the supplied digest, and performs the query under the same fence. It rejects a stale index rather than rebuilding it under the proof, and limits the identity scan to one million rows and 64 MiB of encoded data. It does not support non-SQLite databases; batch requests are fanned out one pinned call per query. The batch response is `{"results": [...]}` with each input `query` preserved; query validation errors stay on that entry. Transport and snapshot-proof failures abort the batch. When a batch is empty or all entries are invalid queries, the pinned adapter executes no query and does not validate batch-wide parameters: it returns an empty `results` list or per-entry errors, while the Python batch endpoint also rejects invalid batch-wide parameters. Those responses contain no corpus data. The pinned protocol also requires a trusted runtime-manifest digest and the warm process generation from `/runtime-generation`. Before and after the query, the backend checks its executable and every currently loaded module file against the owner-provided manifest. The VM command requires all three pins and remains deny-all. This is file integrity under an immutable, trusted supervisor and filesystem, not remote or hardware attestation: the owner must qualify manifest construction before startup, dependencies loaded later, native libraries and binary identity. It is not a production activation.

## `resembl-service` flags

`resembl-service -h` prints the same list. `-resource`, `-index-sha256`, `-artifact-sha256`, `-generation` and `-custody` are required; the process exits at startup when one is missing or invalid.

| Flag | Default | Meaning |
| ---- | ------- | ------- |
| `-listen` | `127.0.0.1:8080` | Listen address. |
| `-engine` | `http://127.0.0.1:8081` | Loopback `resembl serve` URL. |
| `-resource` | none | Server-owned name of the index; the credential is scoped to it. |
| `-index-sha256` | none | Expected SQLite index digest (64 lowercase hex). |
| `-artifact-sha256` | none | Digest of the runtime manifest the engine reads from `RESEMBL_RUNTIME_MANIFEST` (64 lowercase hex). |
| `-generation` | none | Value from the engine's `GET /runtime-generation`; a restarted engine has a new one. |
| `-custody` | none | Private (mode 0700 or stricter), pre-created, persistent directory. |
| `-token-file` | none | Bearer credential file; without it every query is refused. |
| `-concurrency` | `4` | Concurrent queries and custody slots, 1 to 64. |
| `-timeout` | `30s` | Per-request timeout. |

`/v1/find-batch` fans each query through the pinned `/find-pinned` path (one upstream call per query, preserving the snapshot proof per result); it answers `501` only when the configured engine is not a `BatchEngine`.

Run the checks with `make service-check` from the repository root (needs Go 1.27 or newer).

## Container image

Build from the repository root (the Dockerfile's COPY paths are relative
to it, so passing `service/` as the build context fails):

```sh
docker build --platform linux/amd64 -f service/Dockerfile --build-arg VERSION=<tag> .
# For arm64 (e.g. Apple Silicon, Raspberry Pi):
docker build --platform linux/arm64 -f service/Dockerfile --build-arg VERSION=<tag> .
```

The build pins `GOARCH` to the target architecture (`TARGETARCH`), so
the `--platform` selects the binary architecture correctly; omitting
`--platform` builds for the builder's native architecture.

Runs non-root (`USER 65532:65532`), exposes `8080`, and has no shell or
HTTP client in the `scratch` runtime, so `HEALTHCHECK NONE` is deliberate:
probe `GET /healthz` (HTTP 204, no dependency I/O) from your orchestrator
instead. The binary defaults to `127.0.0.1:8080`, which inside a container
is the container's own loopback and unreachable from outside; pass
`-listen 0.0.0.0:8080` to serve on the published port, and keep a trusted
authenticated TLS proxy and network controls in front of a non-loopback
listen address. `-custody` and `-token-file` must be host paths the
operator pre-creates and mounts into the container (mode `0700` or stricter
on the custody directory, readable only by the service for the token file);
the image carries neither, and startup fails without `-custody`. The
`-engine` URL stays loopback relative to the container, so the `resembl
serve` backend must run in the same container or an adjacent one sharing a
network namespace.

The VM/container command waits for active requests to drain on SIGINT or SIGTERM before exiting, with a 30-second shutdown budget. A request still running when that budget expires retains its durable custody claim.
