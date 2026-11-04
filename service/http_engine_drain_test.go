package service

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestEngineErrorBodyDrainedForReuse(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls == 1 {
			w.WriteHeader(http.StatusBadGateway)
			fmt.Fprint(w, `{"error":"engine unavailable"}`)
			return
		}
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, `{"matches":[]}`)
	}))
	defer server.Close()
	engine, err := NewLocalEngine(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := engine.Find(ctx, json.RawMessage(`{"query":"x"}`)); err == nil {
		t.Fatal("expected engine rejection")
	}
	out, err := engine.Find(ctx, json.RawMessage(`{"query":"x"}`))
	if err != nil {
		t.Fatal(err)
	}
	if string(out) != `{"matches":[]}` {
		t.Fatalf("unexpected output %s", out)
	}
	if calls != 2 {
		t.Fatalf("calls = %d", calls)
	}
}
