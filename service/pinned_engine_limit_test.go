package service

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestBatchResponseLimitIncludesTheAssembledEnvelope(t *testing.T) {
	const limit = 8 << 20
	queries := []string{"first", "quoted \"<&>\nquery", "third"}
	index, artifact, generation := strings.Repeat("a", 64), strings.Repeat("b", 64), "fixture"
	for _, delta := range []int{-1, 0, 1} {
		t.Run(map[int]string{-1: "below", 0: "exact", 1: "above"}[delta], func(t *testing.T) {
			rows := make([]map[string]any, len(queries))
			results := make(map[string]map[string]any, len(queries))
			for i, query := range queries {
				result := map[string]any{"lsh_candidates": 1, "matches": []any{map[string]any{"checksum": index, "names": []string{""}, "score": 1}}}
				results[query] = result
				rows[i] = map[string]any{"query": query, "lsh_candidates": result["lsh_candidates"], "matches": result["matches"]}
			}
			baseline, err := json.Marshal(map[string]any{"results": rows})
			if err != nil {
				t.Fatal(err)
			}
			padding := limit + delta - len(baseline)
			for i, query := range queries {
				size := padding / len(queries)
				if i < padding%len(queries) {
					size++
				}
				result := results[query]
				result["matches"].([]any)[0].(map[string]any)["names"] = []string{strings.Repeat("x", size)}
			}
			expected, err := json.Marshal(map[string]any{"results": rows})
			if err != nil {
				t.Fatal(err)
			}
			if len(expected) != limit+delta {
				t.Fatalf("fixture response size=%d want=%d", len(expected), limit+delta)
			}
			backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				if r.URL.Path == "/find-batch" {
					_, _ = w.Write(expected)
					return
				}
				if r.URL.Path != "/find-pinned" {
					t.Errorf("unexpected path %s", r.URL.Path)
					w.WriteHeader(404)
					return
				}
				var request struct {
					Query string `json:"query"`
				}
				if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
					t.Error(err)
					w.WriteHeader(400)
					return
				}
				_ = json.NewEncoder(w).Encode(map[string]any{"index_sha256": index, "artifact_sha256": artifact, "generation": generation, "result": results[request.Query]})
			}))
			defer backend.Close()
			local, err := NewLocalEngine(backend.URL)
			if err != nil {
				t.Fatal(err)
			}
			defer local.CloseIdleConnections()
			pinned, err := NewPinnedSQLiteEngine(backend.URL, index, artifact, generation)
			if err != nil {
				t.Fatal(err)
			}
			defer pinned.CloseIdleConnections()
			body, err := json.Marshal(map[string]any{"queries": queries})
			if err != nil {
				t.Fatal(err)
			}
			for _, adapter := range []struct {
				name   string
				engine BatchEngine
			}{{"local", local}, {"pinned", pinned}} {
				t.Run(adapter.name, func(t *testing.T) {
					got, err := adapter.engine.FindBatch(context.Background(), body)
					if delta > 0 {
						if err == nil || err.Error() != "invalid engine response" || len(got) != 0 {
							t.Fatalf("oversized batch: bytes=%d error=%v", len(got), err)
						}
					} else if err != nil || !bytes.Equal(got, expected) {
						t.Fatalf("bounded batch: bytes=%d want=%d error=%v", len(got), len(expected), err)
					}
				})
			}
		})
	}
}
