package service

import (
	"context"
	"strings"
	"testing"
	"time"
)

func TestNewRejectsInvalidNow(t *testing.T) {
	t.Setenv("RESEMBL_NOW", "not-a-date")
	_, err := New(authFunc(func(context.Context, string, string, string) (bool, error) { return true, nil }), okEngine{}, "index", 1, time.Second)
	if err == nil {
		t.Fatal("expected error for invalid RESEMBL_NOW")
	}
	if !strings.Contains(err.Error(), "RESEMBL_NOW") {
		t.Fatalf("error should name RESEMBL_NOW: %v", err)
	}
}

func TestNewAcceptsValidNow(t *testing.T) {
	t.Setenv("RESEMBL_NOW", "2024-06-01T12:00:00+00:00")
	if _, err := New(authFunc(func(context.Context, string, string, string) (bool, error) { return true, nil }), okEngine{}, "index", 1, time.Second); err != nil {
		t.Fatalf("valid RESEMBL_NOW rejected: %v", err)
	}
}

func TestNewAcceptsValidNowForms(t *testing.T) {
	for _, v := range []string{
		"2024-06-01T12:00:00+0000",
		"2024-06-01T12:00:00+000000",
		"2024-06-01T12:00:00+00",
		"2024-06-01T12:00:00+23:59",
		"2024-06-01T12:00:00+00:00:00",
		"2024-06-01T12:00:00+02:00:30",
		"2024-06-01T12:00:00Z",
		"2024-06-01T12:00:00.123456789+00:00",
	} {
		t.Setenv("RESEMBL_NOW", v)
		if _, err := New(authFunc(func(context.Context, string, string, string) (bool, error) { return true, nil }), okEngine{}, "index", 1, time.Second); err != nil {
			t.Fatalf("RESEMBL_NOW=%q rejected: %v", v, err)
		}
	}
}

func TestNewRejectsOutOfRangeOffsetNow(t *testing.T) {
	for _, v := range []string{
		"2024-06-01T12:00:00+24:00",
		"2024-06-01T12:00:00+24",
		"2024-06-01T12:00:00+2400",
		"2024-06-01T12:00:00+23:60",
		"2024-06-01T12:00:00+2360",
		"2024-06-01T12:00:00+00:00:60",
	} {
		t.Setenv("RESEMBL_NOW", v)
		if _, err := New(authFunc(func(context.Context, string, string, string) (bool, error) { return true, nil }), okEngine{}, "index", 1, time.Second); err == nil {
			t.Fatalf("RESEMBL_NOW=%q accepted, want rejection", v)
		}
	}
}

func TestNewRejectsUnknownOffsetNow(t *testing.T) {
	for _, v := range []string{
		"2024-06-01T12:00:00-00:00",
		"2024-06-01T12:00:00-0000",
		"2024-06-01T12:00:00-00",
		"2024-06-01T12:00:00-00:00:00",
	} {
		t.Setenv("RESEMBL_NOW", v)
		if _, err := New(authFunc(func(context.Context, string, string, string) (bool, error) { return true, nil }), okEngine{}, "index", 1, time.Second); err == nil {
			t.Fatalf("RESEMBL_NOW=%q accepted, want rejection", v)
		}
	}
}

func TestNewRequestIDRejectsInvalidSeed(t *testing.T) {
	t.Setenv("RESEMBL_SEED", "abc")
	defer func() {
		if r := recover(); r == nil {
			t.Fatal("expected panic for invalid RESEMBL_SEED")
		}
	}()
	_ = newRequestID()
}
