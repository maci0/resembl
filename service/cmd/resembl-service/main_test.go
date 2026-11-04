package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// args returns a valid flag set with the overrides applied; an empty override
// value drops the flag.
func args(t *testing.T, overrides map[string]string) []string {
	t.Helper()
	custody := filepath.Join(t.TempDir(), "custody")
	if err := os.Mkdir(custody, 0700); err != nil {
		t.Fatal(err)
	}
	pin := strings.Repeat("a", 64)
	flags := map[string]string{
		"index-sha256": pin, "artifact-sha256": pin, "generation": "g",
		"custody": custody, "resource": "index-1",
	}
	for name, value := range overrides {
		flags[name] = value
	}
	var out []string
	for name, value := range flags {
		if value != "" {
			out = append(out, "-"+name, value)
		}
	}
	return out
}

func TestNewServerAppliesFlags(t *testing.T) {
	server, err := newServer(args(t, map[string]string{
		"listen": "127.0.0.1:9", "timeout": "7s", "concurrency": "2",
	}))
	if err != nil {
		t.Fatal(err)
	}
	if server.Addr != "127.0.0.1:9" || server.ReadTimeout != 7*time.Second || server.WriteTimeout != 12*time.Second {
		t.Fatalf("flags not applied: %+v", server)
	}
}

func TestNewServerRejectsBadConfiguration(t *testing.T) {
	for name, overrides := range map[string]map[string]string{
		"no resource":      {"resource": ""},
		"bad index":        {"index-sha256": "xyz"},
		"zero concurrency": {"concurrency": "0"},
		"zero timeout":     {"timeout": "0s"},
		"missing custody":  {"custody": "/nonexistent-custody"},
		"unknown flag":     {"nope": "1"},
	} {
		if _, err := newServer(args(t, overrides)); err == nil {
			t.Errorf("%s: expected an error", name)
		}
	}
}
