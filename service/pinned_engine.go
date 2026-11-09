package service

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
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
	pin, _ := json.Marshal(e.index)
	artifact, _ := json.Marshal(e.artifact)
	generation, _ := json.Marshal(e.generation)
	results := make([]json.RawMessage, 0, len(queries))
	for _, q := range queries {
		single, _ := json.Marshal(map[string]json.RawMessage{
			"query":           q,
			"index_sha256":    pin,
			"artifact_sha256": artifact,
			"generation":      generation,
		})
		res, err := e.local.find(ctx, "/find-pinned", single)
		if err != nil {
			return nil, err
		}
		var response struct {
			Index      string          `json:"index_sha256"`
			Artifact   string          `json:"artifact_sha256"`
			Generation string          `json:"generation"`
			Result     json.RawMessage `json:"result"`
		}
		if json.Unmarshal(res, &response) != nil || response.Index != e.index || response.Artifact != e.artifact || response.Generation != e.generation || !json.Valid(response.Result) {
			return nil, errors.New("invalid snapshot proof")
		}
		results = append(results, response.Result)
	}
	out, err := json.Marshal(results)
	if err != nil {
		return nil, err
	}
	return out, nil
}
