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

func TestNewRequestIDRejectsInvalidSeed(t *testing.T) {
	t.Setenv("RESEMBL_SEED", "abc")
	defer func() {
		if r := recover(); r == nil {
			t.Fatal("expected panic for invalid RESEMBL_SEED")
		}
	}()
	_ = newRequestID()
}
