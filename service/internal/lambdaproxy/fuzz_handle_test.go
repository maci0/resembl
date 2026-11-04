package lambdaproxy

import (
	"context"
	"encoding/base64"
	"net/http"
	"net/url"
	"strings"
	"testing"

	"github.com/aws/aws-lambda-go/events"
)

// fuzzHandler is a bounded stand-in for the wrapped handler: it writes a
// small fixed response, so the fuzzer spends its budget on the adapter's
// parsing (path, query string, headers, cookies, base64 body) rather than
// on a real engine.
// percentDecode mirrors the decoded path the handler observes: the raw
// path with percent escapes resolved.
func percentDecode(raw string) string {
	if decoded, err := url.PathUnescape(raw); err == nil {
		return decoded
	}
	return raw
}

func fuzzHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Add("Set-Cookie", "a=b")
		w.Header().Set("X-Echo-Path", r.URL.Path)
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	})
}

// FuzzHandle drives Handle with caller-controlled API Gateway event fields
// and asserts the invariants the rest of the service relies on: Handle
// never panics or returns an error, the status is always a real HTTP
// status, and the response body always round-trips through the base64
// encoding Handle promises API Gateway.
func FuzzHandle(f *testing.F) {
	f.Add("POST", "/v1/find", "x=1", `{"query":"ret"}`, false, "Authorization", "Bearer test")
	f.Add("GET", "/healthz", "", "", false, "", "")
	f.Add("POST", "//evil", "", "", false, "", "")
	f.Add("POST", "/v1/find", "", "!!!not-base64!!!", true, "", "")
	f.Add("GET", "/v1/find", "x=1", "", false, "bad\nheader", "x")
	f.Add("", "", "", "", false, "", "")
	h := fuzzHandler()
	f.Fuzz(func(t *testing.T, method, path, query, body string, isBase64 bool, headerName, headerValue string) {
		// Keep the corpus on the interesting side of the size guards: a
		// multi-megabyte body is a length-check case, not a parser case.
		if len(body) > 4096 || len(path) > 4096 || len(query) > 4096 {
			t.Skip("oversized input")
		}
		if method == "" {
			method = http.MethodGet
		}
		event := events.APIGatewayV2HTTPRequest{
			Version:         "2.0",
			RawPath:         path,
			RawQueryString:  query,
			Body:            body,
			IsBase64Encoded: isBase64,
			RequestContext: events.APIGatewayV2HTTPRequestContext{
				HTTP: events.APIGatewayV2HTTPRequestContextHTTPDescription{Method: method},
			},
		}
		if headerName != "" {
			event.Headers = map[string]string{headerName: headerValue}
		}
		out, err := Handle(context.Background(), h, event)
		if err != nil {
			t.Fatalf("Handle returned an error: %v", err)
		}
		if out.StatusCode < 100 || out.StatusCode > 599 {
			t.Fatalf("Handle returned status %d", out.StatusCode)
		}
		// A status below 400 means the handler ran, so its echo of the
		// path it saw must be present in the forwarded headers and its
		// body must round-trip through the base64 encoding Handle
		// promises API Gateway. Rejected requests carry no body at all.
		if out.StatusCode < 400 {
			if !out.IsBase64Encoded {
				t.Fatalf("handler response body was not base64-encoded")
			}
			if _, err := base64.StdEncoding.DecodeString(out.Body); err != nil {
				t.Fatalf("response body does not round-trip base64: %v", err)
			}
			// http.Request.URL.Path is the percent-decoded form of the
			// raw path, so compare against that, not the raw bytes.
			if got, want := out.Headers["X-Echo-Path"], percentDecode(path); got != want {
				t.Fatalf("handler echo %q does not match decoded path %q", got, want)
			}
		}
		_ = strings.Contains
	})
}
