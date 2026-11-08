package lambdaproxy

import (
	"context"
	"net/http"
	"testing"
	"time"

	service "github.com/maci0/resembl/service"
)

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
