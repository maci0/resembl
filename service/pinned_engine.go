package service

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"
)

// PinnedSQLiteEngine requires the backend's query-bound SQLite snapshot check.
// Artifact authenticity is a separate deployment-owner responsibility.
type PinnedSQLiteEngine struct {
	local                       *LocalEngine
	index, artifact, generation string
}

func NewPinnedSQLiteEngine(endpoint, index, artifact, generation string) (*PinnedSQLiteEngine, error) {
	return NewPinnedSQLiteEngineWithTimeout(endpoint, index, artifact, generation, 30*time.Second)
}

// NewPinnedSQLiteEngineWithTimeout is NewPinnedSQLiteEngine with a
// caller-chosen engine timeout (see NewLocalEngineWithTimeout).
func NewPinnedSQLiteEngineWithTimeout(endpoint, index, artifact, generation string, timeout time.Duration) (*PinnedSQLiteEngine, error) {
	raw, err := hex.DecodeString(index)
	if err != nil || len(raw) != 32 || hex.EncodeToString(raw) != index {
		return nil, errors.New("invalid index digest")
	}
	a, err := hex.DecodeString(artifact)
	if err != nil || len(a) != 32 || hex.EncodeToString(a) != artifact || generation == "" {
		return nil, errors.New("invalid runtime pin")
	}
	local, err := NewLocalEngineWithTimeout(endpoint, timeout)
	if err != nil {
		return nil, err
	}
	return &PinnedSQLiteEngine{local, index, artifact, generation}, nil
}

// CloseIdleConnections retires pooled engine connections.  The local
// engine's transport is bounded, but a process that discards the pinned
// engine (shutdown, tests) must still release its idle sockets.
func (e *PinnedSQLiteEngine) CloseIdleConnections() {
	if e.local != nil {
		e.local.CloseIdleConnections()
	}
}

func (e *PinnedSQLiteEngine) Find(ctx context.Context, body json.RawMessage) (json.RawMessage, error) {
	var request map[string]json.RawMessage
	if json.Unmarshal(body, &request) != nil || request == nil {
		return nil, errors.New("invalid request")
	}
	pin, _ := json.Marshal(e.index)
	request["index_sha256"] = pin
	artifact, _ := json.Marshal(e.artifact)
	request["artifact_sha256"] = artifact
	generation, _ := json.Marshal(e.generation)
	request["generation"] = generation
	payload, err := json.Marshal(request)
	if err != nil {
		return nil, err
	}
	raw, err := e.local.find(ctx, "/find-pinned", payload)
	if err != nil {
		return nil, err
	}
	var response struct {
		Index      string          `json:"index_sha256"`
		Artifact   string          `json:"artifact_sha256"`
		Generation string          `json:"generation"`
		Result     json.RawMessage `json:"result"`
	}
	if json.Unmarshal(raw, &response) != nil || response.Index != e.index || response.Artifact != e.artifact || response.Generation != e.generation || !json.Valid(response.Result) {
		return nil, errors.New("invalid snapshot proof")
	}
	return response.Result, nil
}

// FindBatch implements BatchEngine by fanning each query through
// /find-pinned, preserving the snapshot proof for every result.
// shortcut: no valid query means no batch-parameter check; use a pinned validation endpoint when available.
func (e *PinnedSQLiteEngine) FindBatch(ctx context.Context, body json.RawMessage) (json.RawMessage, error) {
	var request map[string]json.RawMessage
	if json.Unmarshal(body, &request) != nil || request == nil {
		return nil, errors.New("invalid request")
	}
	raw, ok := request["queries"]
	if !ok {
		return nil, errors.New("invalid request")
	}
	var queries []json.RawMessage
	if json.Unmarshal(raw, &queries) != nil || queries == nil || len(queries) > 1000 {
		return nil, errors.New("invalid request")
	}
	// Carry the find parameters (top_n, threshold, normalize, ...) into
	// every fanned-out single: a batch that names them must not silently
	// search with the engine defaults instead.
	params := make(map[string]json.RawMessage, len(request))
	for key, value := range request {
		if key != "queries" {
			params[key] = value
		}
	}
	results := make([]map[string]json.RawMessage, 0, len(queries))
	for _, q := range queries {
		single := make(map[string]json.RawMessage, len(params)+4)
		for key, value := range params {
			single[key] = value
		}
		// Find overwrites every proof field before dispatch.
		single["query"] = q
		singleBytes, marshalErr := json.Marshal(single)
		if marshalErr != nil {
			return nil, marshalErr
		}
		res, err := e.Find(ctx, singleBytes)
		entry := make(map[string]json.RawMessage)
		if err != nil {
			var rejection *EngineError
			if !errors.As(err, &rejection) || rejection.Status != http.StatusBadRequest {
				return nil, err
			}
			reason := strings.TrimPrefix(rejection.Message, "bad request: ")
			switch reason {
			case "query must be a string", "query must not be empty", "query input is not valid UTF-8":
				entry["error"], _ = json.Marshal(reason)
			default:
				return nil, err
			}
		} else if json.Unmarshal(res, &entry) != nil || entry == nil {
			return nil, errors.New("invalid batch result")
		}
		entry["query"] = q
		results = append(results, entry)
	}
	return json.Marshal(map[string]any{"results": results})
}
