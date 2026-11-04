package service

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// fuzzEngine is a bounded stand-in for the wrapped engine: it returns a
// small fixed JSON result, so the fuzzer spends its budget on the service
// boundary's parsing (method, path, headers, content type, JSON body)
// rather than on a real engine.
type fuzzEngine struct{}

func (fuzzEngine) Find(context.Context, json.RawMessage) (json.RawMessage, error) {
	return json.RawMessage(`{"matches":[]}`), nil
}

func (fuzzEngine) FindBatch(context.Context, json.RawMessage) (json.RawMessage, error) {
	return json.RawMessage(`{"results":[]}`), nil
}

// FuzzServeHTTP drives Service.ServeHTTP with caller-controlled method,
// path, content type, body and authorization header. It asserts the
// invariants callers rely on: ServeHTTP never panics, it always answers
// with a real HTTP status, any error answer carries the JSON error
// envelope, and a 2xx answer to /v1/find or /v1/find-batch carries a
// valid JSON body shape (a string `matches` field check is per-endpoint:
// the engine result is forwarded verbatim, so the harness only requires
// the body to be valid JSON, which writeError and the engine path both
// guarantee).
func FuzzServeHTTP(f *testing.F) {
	f.Add("POST", "/v1/find", "application/json", `{"query":"ret"}`, "Bearer valid")
	f.Add("POST", "/v1/find-batch", "application/json", `{"queries":["ret"]}`, "Bearer valid")
	f.Add("GET", "/healthz", "", "", "")
	f.Add("POST", "/v1/find", "application/json", `{"query":123}`, "Bearer valid")
	f.Add("POST", "/v1/find", "application/json", `{"query":"ret"}`, "")
	f.Add("POST", "/unknown", "text/plain", "not json", "Bearer valid")
	f.Add("POST", "/v1/find", "application/json", `{"query":`, "Bearer valid")
	f.Add("", "", "application/json", "", "Bearer valid")

	svc, err := New(
		authFunc(func(context.Context, string, string, string) (bool, error) { return true, nil }),
		fuzzEngine{},
		"fixture",
		4,
		time.Second,
	)
	if err != nil {
		f.Fatal(err)
	}
	if err = svc.SetCustody(testCustody{}); err != nil {
		f.Fatal(err)
	}

	f.Fuzz(func(t *testing.T, method, path, contentType, body, auth string) {
		// Keep the corpus on the interesting side of the size guards: a
		// multi-megabyte body is a length-check case, not a parser case.
		if len(body) > 4096 || len(path) > 4096 {
			t.Skip("oversized input")
		}
		if method == "" {
			method = http.MethodGet
		}
		if path == "" {
			path = "/"
		}
		// http.NewRequest (not httptest.NewRequest) returns an error for
		// methods/paths the transport would reject: that is exactly the
		// input httptest.NewRequest panics on, so skip it instead.
		target := path
		if !strings.HasPrefix(target, "/") {
			target = "/" + target
		}
		req, err := http.NewRequest(method, "http://fixture"+target, strings.NewReader(body))
		if err != nil {
			t.Skip("unparseable request target")
		}
		if contentType != "" {
			req.Header.Set("Content-Type", contentType)
		}
		if auth != "" {
			req.Header.Set("Authorization", auth)
		}
		w := httptest.NewRecorder()
		svc.ServeHTTP(w, req)

		if w.Code < 100 || w.Code > 599 {
			t.Fatalf("ServeHTTP returned status %d", w.Code)
		}
		if w.Code >= 400 {
			// Every error answer is the JSON envelope; anything else is
			// an unhandled panic or a raw stdlib error page.
			var env map[string]json.RawMessage
			if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil || env["error"] == nil {
				t.Fatalf("error response is not the JSON envelope: %d %q", w.Code, w.Body.String())
			}
			return
		}
		// A 2xx on the find endpoints forwards the engine result verbatim;
		// it must still be valid JSON. Other 2xx paths (/healthz 204,
		// /metrics text) carry no JSON obligation.
		if w.Code < 300 && (path == "/v1/find" || path == "/v1/find-batch") {
			if !json.Valid(w.Body.Bytes()) {
				t.Fatalf("find success body is not valid JSON: %q", w.Body.String())
			}
		}
	})
}
