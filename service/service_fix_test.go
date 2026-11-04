package service

import (
	"context"
	"encoding/json"
	"errors"
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
