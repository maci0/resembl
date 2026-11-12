package lambdaproxy

import (
	"context"
	"encoding/base64"
	"io"
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

func TestOnlyExactHeadSuppressesTheHTTPBody(t *testing.T) {
	e := &engine{}
	handler, err := service.New(service.DenyAll{}, e, "fixture", 1, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(handler)
	defer server.Close()
	for _, method := range []string{"HEAD", "head", "Head", "hEaD"} {
		t.Run(method, func(t *testing.T) {
			request, err := http.NewRequest(method, server.URL+"/v1/find", nil)
			if err != nil {
				t.Fatal(err)
			}
			direct, err := server.Client().Do(request)
			if err != nil {
				t.Fatal(err)
			}
			body, readErr := io.ReadAll(direct.Body)
			closeErr := direct.Body.Close()
			if readErr != nil || closeErr != nil {
				t.Fatalf("direct response body: %v %v", readErr, closeErr)
			}
			adapted, err := Handle(context.Background(), handler, event(method, "/v1/find"))
			if err != nil {
				t.Fatal(err)
			}
			decoded, err := base64.StdEncoding.DecodeString(adapted.Body)
			if err != nil {
				t.Fatal(err)
			}
			if adapted.StatusCode != direct.StatusCode || string(decoded) != string(body) || adapted.Headers["Allow"] != direct.Header.Get("Allow") {
				t.Fatalf("method %q: adapter %d/%q differs from HTTP %d/%q", method, adapted.StatusCode, decoded, direct.StatusCode, body)
			}
		})
	}
}
