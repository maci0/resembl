package service

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// The per-request timeout is what bounds an in-flight engine call; it must
// be the value the caller asked for, not a hardcoded 30s.  A handler with
// a 5s deadline and a 30s client can outlive the custody claim it owns.
func TestLocalEngineTimeoutIsCallerChosen(t *testing.T) {
	e, err := NewLocalEngineWithTimeout("http://127.0.0.1:8081", 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if e.client.Timeout != 5*time.Second {
		t.Fatalf("client timeout = %v, want 5s", e.client.Timeout)
	}
}

func TestLocalEngineDefaultTimeoutIs30s(t *testing.T) {
	e, err := NewLocalEngine("http://127.0.0.1:8081")
	if err != nil {
		t.Fatal(err)
	}
	if e.client.Timeout != 30*time.Second {
		t.Fatalf("default client timeout = %v, want 30s", e.client.Timeout)
	}
}

func TestLocalEngineTruncatedRejectionIsTransportFailure(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Length", "100")
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"error":"invalid query"}`))
	}))
	defer backend.Close()
	engine, err := NewLocalEngine(backend.URL)
	if err != nil {
		t.Fatal(err)
	}
	defer engine.CloseIdleConnections()
	_, err = engine.Find(context.Background(), json.RawMessage(`{"query":"ret"}`))
	var rejection *EngineError
	if !errors.Is(err, io.ErrUnexpectedEOF) || errors.As(err, &rejection) {
		t.Fatalf("truncated rejection = %v, want transport failure", err)
	}
}
