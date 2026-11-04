package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/aws/aws-lambda-go/events"
)

func TestBootstrapRuntimeDenyAll(t *testing.T) {
	binary := filepath.Join(t.TempDir(), "bootstrap")
	build := exec.Command("go", "build", "-o", binary, ".")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build failed: %s", out)
	}
	responses := make(chan events.APIGatewayV2HTTPResponse, 1)
	var next atomic.Int32
	runtime := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/runtime/invocation/next"):
			if next.Add(1) > 1 {
				<-r.Context().Done()
				return
			}
			w.Header().Set("Lambda-Runtime-Aws-Request-Id", "fixture-request")
			w.Header().Set("Lambda-Runtime-Deadline-Ms", fmt.Sprint(time.Now().Add(30*time.Second).UnixMilli()))
			_, _ = io.WriteString(w, `{"version":"2.0","rawPath":"/v1/find","requestContext":{"http":{"method":"POST"}},"headers":{"authorization":"Bearer fixture"},"body":"{\"query\":\"x\"}"}`)
		case strings.HasSuffix(r.URL.Path, "/runtime/invocation/fixture-request/response"):
			var out events.APIGatewayV2HTTPResponse
			if json.NewDecoder(r.Body).Decode(&out) != nil {
				t.Error("bad runtime response")
			}
			responses <- out
			w.WriteHeader(http.StatusAccepted)
		default:
			http.Error(w, "unexpected", 500)
		}
	}))
	defer runtime.Close()
	cmd := exec.Command(binary)
	cmd.Env = append(os.Environ(), "AWS_LAMBDA_RUNTIME_API="+strings.TrimPrefix(runtime.URL, "http://"), "_HANDLER=bootstrap")
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = cmd.Process.Kill(); _ = cmd.Wait() }()
	select {
	case out := <-responses:
		if out.StatusCode != http.StatusForbidden {
			t.Fatalf("expected deny, got %d", out.StatusCode)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("runtime response timeout")
	}
}
