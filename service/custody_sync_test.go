//go:build unix

package service

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestCustodySyncOpenFailurePreventsDispatch(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "custody")
	if err := os.Mkdir(dir, 0300); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = os.Chmod(dir, 0700) }()
	custody, err := NewFileCustody(dir, 1)
	if err != nil {
		t.Fatal(err)
	}
	handler, err := New(authFunc(func(context.Context, string, string, string) (bool, error) { return true, nil }), fuzzEngine{}, "index", 1, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err := handler.SetCustody(custody); err != nil {
		t.Fatal(err)
	}
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/v1/find", strings.NewReader(`{"query":"ret"}`)))
	if response.Code != http.StatusServiceUnavailable {
		t.Fatalf("unsynced claim dispatched: %d %s", response.Code, response.Body)
	}
	var envelope map[string]string
	if err := json.Unmarshal(response.Body.Bytes(), &envelope); err != nil || envelope["error"] != "capacity unavailable" {
		t.Fatalf("capacity refusal: %s, %v", response.Body, err)
	}
	if _, err := os.Stat(filepath.Join(dir, "slot-00")); err != nil {
		t.Fatalf("failed claim was not retained: %v", err)
	}
}

func TestCustodyReleasePropagatesSyncOpenFailure(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "custody")
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = os.Chmod(dir, 0700) }()
	custody, err := NewFileCustody(dir, 1)
	if err != nil {
		t.Fatal(err)
	}
	release, err := custody.Claim()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(dir, 0300); err != nil {
		t.Fatal(err)
	}
	if err := release(); !errors.Is(err, os.ErrPermission) {
		t.Fatalf("release hid sync open failure: %v", err)
	}
}
