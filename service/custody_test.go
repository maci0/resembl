package service

import (
	"context"
	"encoding/json"
	"errors"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
)

type uncertainEngine struct{}

func (uncertainEngine) Find(context.Context, json.RawMessage) (json.RawMessage, error) {
	return nil, errors.New("lost response")
}
func TestCustodySurvivesRestartAfterUnknownOutcome(t *testing.T) {
	dir := t.TempDir()
	if err := os.Chmod(dir, 0700); err != nil {
		t.Fatal(err)
	}
	c, err := NewFileCustody(dir, 1)
	if err != nil {
		t.Fatal(err)
	}
	s, err := New(authFunc(func(context.Context, string, string, string) (bool, error) { return true, nil }), uncertainEngine{}, "index", 1, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err = s.SetCustody(c); err != nil {
		t.Fatal(err)
	}
	w := httptest.NewRecorder()
	s.ServeHTTP(w, httptest.NewRequest("POST", "/v1/find", strings.NewReader(`{"query":"ret"}`)))
	if w.Code != 503 {
		t.Fatal(w.Code)
	}
	recovered, err := NewFileCustody(dir, 1)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = recovered.Claim(); !errors.Is(err, ErrCapacity) {
		t.Fatalf("unknown execution lost after restart: %v", err)
	}
}
func TestCustodyDefiniteCompletionReleases(t *testing.T) {
	dir := t.TempDir()
	if err := os.Chmod(dir, 0700); err != nil {
		t.Fatal(err)
	}
	c, err := NewFileCustody(dir, 1)
	if err != nil {
		t.Fatal(err)
	}
	release, err := c.Claim()
	if err != nil {
		t.Fatal(err)
	}
	if _, err = c.Claim(); !errors.Is(err, ErrCapacity) {
		t.Fatal("capacity not bounded")
	}
	if err = release(); err != nil {
		t.Fatal(err)
	}
	again, err := c.Claim()
	if err != nil {
		t.Fatal(err)
	}
	if err = again(); err != nil {
		t.Fatal(err)
	}
}
