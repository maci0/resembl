package service

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestPinnedBatchForwardsFindParameters(t *testing.T) {
	pin := strings.Repeat("a", 64)
	var gotBodies []map[string]any
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Errorf("decode: %v", err)
		}
		gotBodies = append(gotBodies, body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"index_sha256":"` + pin + `","artifact_sha256":"` + pin + `","generation":"generation","result":{"matches":[]}}`))
	}))
	defer backend.Close()
	engine, err := NewPinnedSQLiteEngine(backend.URL, pin, pin, "generation")
	if err != nil {
		t.Fatal(err)
	}
	_, err = engine.FindBatch(context.Background(), json.RawMessage(`{"queries":["ret","nop"],"top_n":5,"threshold":0.9,"normalize":false}`))
	if err != nil {
		t.Fatal(err)
	}
	if len(gotBodies) != 2 {
		t.Fatalf("backend calls %d, want 2", len(gotBodies))
	}
	for i, body := range gotBodies {
		if v, ok := body["top_n"]; !ok || v != float64(5) {
			t.Errorf("call %d top_n=%v, want 5 (find parameters must reach /find-pinned)", i, body["top_n"])
		}
		if v, ok := body["threshold"]; !ok || v != float64(0.9) {
			t.Errorf("call %d threshold=%v, want 0.9", i, body["threshold"])
		}
		if v, ok := body["normalize"]; !ok || v != false {
			t.Errorf("call %d normalize=%v, want false", i, body["normalize"])
		}
	}
}
