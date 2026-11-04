package service

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestTokenAuthorityRequiresExactCredentialAndScope(t *testing.T) {
	path := filepath.Join(t.TempDir(), "token")
	token := strings.Repeat("a1", 32)
	if err := os.WriteFile(path, []byte(token+"\n"), 0600); err != nil {
		t.Fatal(err)
	}
	auth, err := NewTokenAuthority(path, "index-A")
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		credential, resource, verb string
		want                       bool
	}{
		{"Bearer " + token, "index-A", "query", true},
		{"Bearer " + strings.Repeat("a2", 32), "index-A", "query", false},
		{"Bearer " + token, "index-B", "query", false},
		{"Bearer " + token, "index-A", "write", false},
		{"bearer " + token, "index-A", "query", false},
	} {
		got, err := auth.Allow(context.Background(), tc.credential, tc.resource, tc.verb)
		if err != nil || got != tc.want {
			t.Fatalf("%q/%q: %v %v", tc.resource, tc.verb, got, err)
		}
	}
	if err := os.Chmod(path, 0644); err != nil {
		t.Fatal(err)
	}
	if _, err := NewTokenAuthority(path, "index-A"); err == nil {
		t.Fatal("world-readable credential accepted")
	}
	if err := os.Chmod(path, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("weak"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := NewTokenAuthority(path, "index-A"); err == nil {
		t.Fatal("weak credential accepted")
	}
}

func TestTokenAuthorityProtectsHTTPQueries(t *testing.T) {
	path := filepath.Join(t.TempDir(), "token")
	token := strings.Repeat("ab", 32)
	if err := os.WriteFile(path, []byte(token), 0600); err != nil {
		t.Fatal(err)
	}
	auth, err := NewTokenAuthority(path, "index-A")
	if err != nil {
		t.Fatal(err)
	}
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte(`{"matches":[]}`)) }))
	defer backend.Close()
	engine, err := NewLocalEngine(backend.URL)
	if err != nil {
		t.Fatal(err)
	}
	handler, err := New(auth, engine, "index-A", 1, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err := handler.SetCustody(testCustody{}); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		credential string
		status     int
	}{{"", 403}, {"Bearer " + token, 200}} {
		req := httptest.NewRequest("POST", "/v1/find", strings.NewReader(`{"query":"ret"}`))
		req.Header.Set("Authorization", tc.credential)
		result := httptest.NewRecorder()
		handler.ServeHTTP(result, req)
		if result.Code != tc.status {
			t.Fatalf("status %d want %d", result.Code, tc.status)
		}
	}
}
