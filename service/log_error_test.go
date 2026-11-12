package service

import (
	"bytes"
	"context"
	"encoding/json"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestEngineErrorsCannotForgeLogRecords(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "worker\r\nforged\t\x1b[31m\x7f"})
	}))
	defer backend.Close()
	engine, err := NewLocalEngine(backend.URL)
	if err != nil {
		t.Fatal(err)
	}
	defer engine.CloseIdleConnections()
	handler, err := New(authFunc(func(context.Context, string, string, string) (bool, error) { return true, nil }), engine, "index", 1, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err := handler.SetCustody(testCustody{}); err != nil {
		t.Fatal(err)
	}
	var captured bytes.Buffer
	previous := log.Writer()
	log.SetOutput(&captured)
	defer log.SetOutput(previous)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/v1/find", strings.NewReader(`{"query":"ret"}`)))
	if response.Code != http.StatusServiceUnavailable {
		t.Fatalf("status=%d, body=%s", response.Code, response.Body)
	}
	text := captured.String()
	if strings.ContainsAny(text, "\r\t\x1b\x7f") || strings.Count(text, "\n") != 2 {
		t.Fatalf("engine error escaped its log record: %q", text)
	}
	if !strings.Contains(text, "workerforged[31m") {
		t.Fatalf("engine detail missing from log: %q", text)
	}
}
