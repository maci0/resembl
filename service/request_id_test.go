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

func requestIDService(t *testing.T, forwarded *string) *Service {
	t.Helper()
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if forwarded != nil {
			*forwarded = r.Header.Get("X-Request-Id")
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"matches":[]}`))
	}))
	t.Cleanup(backend.Close)
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
	return s
}

func TestSuppliedRequestIDIsAdopted(t *testing.T) {
	var forwarded string
	s := requestIDService(t, &forwarded)
	req := httptest.NewRequest("POST", "/v1/find", strings.NewReader(`{"query":"ret"}`))
	req.Header.Set("X-Request-Id", "upstream-42")
	w := httptest.NewRecorder()
	s.ServeHTTP(w, req)
	if w.Code != 200 {
		t.Fatalf("status %d, want 200", w.Code)
	}
	if got := w.Header().Get("X-Request-Id"); got != "upstream-42" {
		t.Fatalf("response X-Request-Id %q, want upstream-42", got)
	}
	if forwarded != "upstream-42" {
		t.Fatalf("engine saw X-Request-Id %q, want upstream-42", forwarded)
	}
}

func TestOversizedOrControlCharRequestIDIsReminted(t *testing.T) {
	s := requestIDService(t, nil)
	for _, bad := range []string{strings.Repeat("x", 65), "bad\x7fid", "bad\nid"} {
		req := httptest.NewRequest("POST", "/v1/find", strings.NewReader(`{"query":"ret"}`))
		req.Header.Set("X-Request-Id", bad)
		w := httptest.NewRecorder()
		s.ServeHTTP(w, req)
		if w.Code != 200 {
			t.Fatalf("status %d, want 200", w.Code)
		}
		if got := w.Header().Get("X-Request-Id"); got == "" || got == bad || strings.Contains(got, "\n") {
			t.Fatalf("response X-Request-Id %q for bad id %q", got, bad)
		}
	}
}
