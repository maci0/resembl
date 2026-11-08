package service

import (
	"context"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestPinnedClockZeroesLatency(t *testing.T) {
	t.Setenv("RESEMBL_NOW", "2024-06-01T12:00:00+00:00")
	s, err := New(authFunc(func(context.Context, string, string, string) (bool, error) { return true, nil }), okEngine{}, "index", 1, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err := s.SetCustody(testCustody{}); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		w := httptest.NewRecorder()
		s.ServeHTTP(w, httptest.NewRequest("POST", "/v1/find", strings.NewReader(`{"query":"ret"}`)))
		if w.Code != 200 {
			t.Fatalf("status %d, want 200", w.Code)
		}
		// Under RESEMBL_NOW the request latency must be 0, like Python
		// monotonic_now; a wall-clock elapsed would be >0 and non-reproducible.
		for label, sum := range s.metrics.sums {
			if sum != 0 {
				t.Fatalf("pinned latency for %s = %v, want 0", label, sum)
			}
		}
	}
	out := s.metrics.render()
	if !strings.Contains(out, "resembl_service_uptime_seconds 0.000") {
		t.Fatalf("uptime not pinned: %s", out)
	}
}
