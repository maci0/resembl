package lambdaproxy

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	service "github.com/maci0/resembl/service"
)

func TestHeadPreservesHandlerContentLength(t *testing.T) {
	h := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.ServeContent(w, r, "snippet.txt", time.Time{}, strings.NewReader("ret\n"))
	})
	server := httptest.NewServer(h)
	defer server.Close()
	request, err := http.NewRequest(http.MethodHead, server.URL, nil)
	if err != nil {
		t.Fatal(err)
	}
	response, err := server.Client().Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	out, err := Handle(context.Background(), h, event(http.MethodHead, "/snippet.txt"))
	if err != nil || out.StatusCode != response.StatusCode || out.Body != "" || out.Headers["Content-Length"] != response.Header.Get("Content-Length") {
		t.Fatalf("HEAD metadata changed: gateway=%#v, HTTP length=%q, error=%v", out, response.Header.Get("Content-Length"), err)
	}
}

func TestHeadCarriesNoBodyFile(t *testing.T) {
	e := &engine{}
	h, err := service.New(service.DenyAll{}, e, "fixture", 1, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	out, err := Handle(context.Background(), h, event("HEAD", "/v1/find"))
	if err != nil || out.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("head status: %d %v", out.StatusCode, err)
	}
	if out.Body != "" {
		t.Fatalf("head body leaked: %q", out.Body)
	}
	if out.Headers["Allow"] != "POST" {
		t.Fatalf("head Allow: %q", out.Headers["Allow"])
	}
	if out.Headers["Content-Length"] == "" || out.Headers["Content-Length"] == "0" {
		t.Fatalf("head Content-Length missing: %q", out.Headers["Content-Length"])
	}
}
