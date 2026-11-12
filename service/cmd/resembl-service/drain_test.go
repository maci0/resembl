//go:build !windows && !js && !wasip1

package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestMainDrainsBeforeExit(t *testing.T) {
	if os.Getenv("RESEMBL_TEST_DRAIN_CHILD") == "1" {
		os.Args = append(os.Args[:1], os.Args[3:]...)
		main()
		return
	}
	entered := make(chan struct{})
	unblock := make(chan struct{})
	var once sync.Once
	release := func() { once.Do(func() { close(unblock) }) }
	pin := strings.Repeat("a", 64)
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		close(entered)
		<-unblock
		_ = json.NewEncoder(w).Encode(map[string]any{"index_sha256": pin, "artifact_sha256": pin, "generation": "g", "result": map[string]any{"matches": []any{}}})
	}))
	defer backend.Close()
	defer release()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	_ = listener.Close()
	token := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(token, []byte(pin), 0600); err != nil {
		t.Fatal(err)
	}
	custody := t.TempDir()
	if err := os.Chmod(custody, 0700); err != nil {
		t.Fatal(err)
	}
	flags := args(t, map[string]string{"listen": address, "engine": backend.URL, "token-file": token, "custody": custody})
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	command := exec.Command(executable, append([]string{"-test.run=^TestMainDrainsBeforeExit$", "--"}, flags...)...)
	command.Env = append(os.Environ(), "RESEMBL_TEST_DRAIN_CHILD=1")
	var logs bytes.Buffer
	command.Stdout, command.Stderr = &logs, &logs
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = command.Process.Kill() }()
	done := make(chan error, 1)
	go func() { done <- command.Wait() }()
	client := &http.Client{Timeout: 20 * time.Second}
	ready := false
	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		response, err := client.Get("http://" + address + "/healthz")
		if err == nil {
			_ = response.Body.Close()
			ready = true
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if !ready {
		t.Fatal("child did not start")
	}
	result := make(chan error, 1)
	go func() {
		request, err := http.NewRequest("POST", "http://"+address+"/v1/find", strings.NewReader(`{"query":"ret"}`))
		if err != nil {
			result <- err
			return
		}
		request.Header.Set("Authorization", "Bearer "+pin)
		response, err := client.Do(request)
		if err == nil {
			var body []byte
			body, err = io.ReadAll(response.Body)
			_ = response.Body.Close()
			if err == nil && (response.StatusCode != http.StatusOK || string(body) != `{"matches":[]}`) {
				err = fmt.Errorf("query response = %d %s", response.StatusCode, body)
			}
		}
		result <- err
	}()
	select {
	case <-entered:
	case <-time.After(20 * time.Second):
		t.Fatal("backend did not receive query")
	}
	if err := command.Process.Signal(shutdownSIGTERM); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-done:
		t.Fatalf("main exited before backend completed: %v; %s", err, logs.String())
	case <-time.After(200 * time.Millisecond):
	}
	release()
	select {
	case err := <-result:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(20 * time.Second):
		t.Fatal("query did not complete")
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("child exit: %v; %s", err, logs.String())
		}
	case <-time.After(20 * time.Second):
		t.Fatal("child did not stop after drain")
	}
	entries, err := os.ReadDir(custody)
	if err != nil || len(entries) != 0 {
		t.Fatalf("custody after completed query: %v, %v", entries, err)
	}
}
