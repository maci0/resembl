package service

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

type okEngine struct{}

func (okEngine) Find(_ context.Context, _ json.RawMessage) (json.RawMessage, error) {
	return json.RawMessage(`{"matches":[]}`), nil
}

type errBody struct{ err error }

func (e *errBody) Read([]byte) (int, error) { return 0, e.err }
func (e *errBody) Close() error             { return nil }

func TestBodyReadErrorIsNot413(t *testing.T) {
	s, err := New(authFunc(func(context.Context, string, string, string) (bool, error) { return true, nil }), okEngine{}, "index", 1, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err = s.SetCustody(testCustody{}); err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest("POST", "/v1/find", strings.NewReader(`{"query":"ret"}`))
	req.Body = &errBody{errors.New("connection reset")}
	w := httptest.NewRecorder()
	s.ServeHTTP(w, req)
	if w.Code != 400 {
		t.Fatalf("read error gave %d, want 400 (not misreported as 413)", w.Code)
	}
}

func TestReleaseErrorSurfaces(t *testing.T) {
	custody := &failReleaseCustody{}
	s, err := New(authFunc(func(context.Context, string, string, string) (bool, error) { return true, nil }), okEngine{}, "index", 1, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err = s.SetCustody(custody); err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest("POST", "/v1/find", strings.NewReader(`{"query":"ret"}`))
	w := httptest.NewRecorder()
	s.ServeHTTP(w, req)
	if w.Code != 503 {
		t.Fatalf("release failure gave %d, want 503 (capacity unaccounted)", w.Code)
	}
}

type failReleaseCustody struct{}

func (*failReleaseCustody) Claim() (func() error, error) {
	return func() error { return errors.New("slot remove failed") }, nil
}

func TestEngineClientErrorIsRelayedAsItsStatus(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"error":"top_n must be at most 1000"}`))
	}))
	defer backend.Close()
	engine, err := NewLocalEngine(backend.URL)
	if err != nil {
		t.Fatal(err)
	}
	s, err := New(authFunc(func(context.Context, string, string, string) (bool, error) { return true, nil }), engine, "index", 1, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err = s.SetCustody(testCustody{}); err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest("POST", "/v1/find", strings.NewReader(`{"query":"ret","top_n":99999}`))
	w := httptest.NewRecorder()
	s.ServeHTTP(w, req)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("engine 400 gave %d, want 400 (not masked as 503)", w.Code)
	}
	var env map[string]string
	if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil || env["error"] != "top_n must be at most 1000" {
		t.Fatalf("relayed envelope differs: %s", w.Body.String())
	}
}

func TestEngineServerErrorStays503(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadGateway)
		_, _ = w.Write([]byte(`{"error":"engine unavailable"}`))
	}))
	defer backend.Close()
	engine, err := NewLocalEngine(backend.URL)
	if err != nil {
		t.Fatal(err)
	}
	s, err := New(authFunc(func(context.Context, string, string, string) (bool, error) { return true, nil }), engine, "index", 1, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err = s.SetCustody(testCustody{}); err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest("POST", "/v1/find", strings.NewReader(`{"query":"ret"}`))
	w := httptest.NewRecorder()
	s.ServeHTTP(w, req)
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("engine 502 gave %d, want 503", w.Code)
	}
	var env map[string]string
	if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil || env["error"] != "engine unavailable" {
		t.Fatalf("503 envelope differs: %s", w.Body.String())
	}
}

func TestInvalidUTF8BodyIsRejected(t *testing.T) {
	s, err := New(authFunc(func(context.Context, string, string, string) (bool, error) { return true, nil }), fuzzEngine{}, "index", 1, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err := s.SetCustody(testCustody{}); err != nil {
		t.Fatal(err)
	}
	for path, body := range map[string]string{
		"/v1/find":       "{\"query\":\"ret\xff\"}",
		"/v1/find-batch": "{\"queries\":[\"ret\xff\"]}",
	} {
		t.Run(path, func(t *testing.T) {
			request := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
			response := httptest.NewRecorder()
			s.ServeHTTP(response, request)
			if response.Code != http.StatusBadRequest {
				t.Fatalf("invalid UTF-8: status=%d body=%s", response.Code, response.Body)
			}
			var envelope map[string]string
			if err := json.Unmarshal(response.Body.Bytes(), &envelope); err != nil || envelope["error"] != "bad request body" {
				t.Fatalf("bad request envelope: %s, %v", response.Body, err)
			}
		})
	}
}

func TestInvalidUTF8EngineResponseIsRejected(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("{\"matches\":[],\"note\":\"\xff\"}"))
	}))
	defer backend.Close()
	engine, err := NewLocalEngine(backend.URL)
	if err != nil {
		t.Fatal(err)
	}
	defer engine.CloseIdleConnections()
	s, err := New(authFunc(func(context.Context, string, string, string) (bool, error) { return true, nil }), engine, "index", 1, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err := s.SetCustody(testCustody{}); err != nil {
		t.Fatal(err)
	}
	response := httptest.NewRecorder()
	s.ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/v1/find", strings.NewReader(`{"query":"ret"}`)))
	if response.Code != http.StatusBadGateway {
		t.Fatalf("invalid UTF-8 result: status=%d body=%q", response.Code, response.Body.Bytes())
	}
	var envelope map[string]string
	if err := json.Unmarshal(response.Body.Bytes(), &envelope); err != nil || envelope["error"] != "invalid engine response" {
		t.Fatalf("bad gateway envelope: %s, %v", response.Body, err)
	}
}
