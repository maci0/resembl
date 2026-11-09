package service

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// TestMetricsRequiresQueryAuthorization pins the deny side of the matrix:
// /metrics must not be readable without the same credential that can query,
// while /healthz remains open for probes.
func TestMetricsRequiresQueryAuthorization(t *testing.T) {
	deny, err := New(authFunc(func(context.Context, string, string, string) (bool, error) { return false, nil }), okEngine{}, "index", 1, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err = deny.SetCustody(testCustody{}); err != nil {
		t.Fatal(err)
	}
	w := httptest.NewRecorder()
	deny.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if w.Code != http.StatusForbidden {
		t.Fatalf("unauthenticated /metrics gave %d, want 403", w.Code)
	}
	w = httptest.NewRecorder()
	deny.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if w.Code != http.StatusNoContent {
		t.Fatalf("/healthz gave %d, want 204 (probes stay unauthenticated)", w.Code)
	}

	allow, err := New(authFunc(func(context.Context, string, string, string) (bool, error) { return true, nil }), okEngine{}, "index", 1, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err = allow.SetCustody(testCustody{}); err != nil {
		t.Fatal(err)
	}
	w = httptest.NewRecorder()
	allow.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("authorized /metrics gave %d, want 200", w.Code)
	}
}
