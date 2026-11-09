package service

import (
	"context"
	"testing"
	"time"
)

// Python parse_instant truth table (resembl/clock.py): the Go mirror must
// agree, since the same RESEMBL_NOW validates in one process, fails fast
// in the other.
func TestNewNowValidationAgreesWithPythonOffsets(t *testing.T) {
	valid := []string{
		"2024-06-01T12:00:00+00:00:00",
		"2024-06-01T12:00:00+000000",
		"2024-06-01T12:00:00+00",
		"2024-06-01T12:00:00+0530",
		"2024-06-01T12:00:00+0000",
		"2024-06-01T12:00:00Z",
		"2024-06-01T12:00:00.123456789+00:00",
	}
	invalid := []string{"2024-06-01T12:00:00+24:00", "2024-06-01T12:00:00+23:60"}
	for _, v := range valid {
		t.Setenv("RESEMBL_NOW", v)
		if _, err := New(authFunc(func(context.Context, string, string, string) (bool, error) { return true, nil }), okEngine{}, "index", 1, time.Second); err != nil {
			t.Errorf("RESEMBL_NOW=%q rejected, Python accepts", v)
		}
	}
	for _, v := range invalid {
		t.Setenv("RESEMBL_NOW", v)
		if _, err := New(authFunc(func(context.Context, string, string, string) (bool, error) { return true, nil }), okEngine{}, "index", 1, time.Second); err == nil {
			t.Errorf("RESEMBL_NOW=%q accepted, Python rejects", v)
		}
	}
}
