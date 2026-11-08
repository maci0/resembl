package service

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestRequestIDForwardedToEngine(t *testing.T) {
	var forwarded string
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		forwarded = r.Header.Get("X-Request-Id")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"matches":[]}`))
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
	if w.Code != 200 {
		t.Fatalf("status %d, want 200", w.Code)
	}
	// The id the client got back is the id the engine received: one
	// correlation id end to end, not one minted per hop.
	if got := w.Header().Get("X-Request-Id"); got == "" || got != forwarded {
		t.Fatalf("response X-Request-Id %q != engine saw %q", got, forwarded)
	}
}
