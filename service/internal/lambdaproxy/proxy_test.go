package lambdaproxy

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-lambda-go/events"
	service "github.com/maci0/resembl/service"
)

type engine struct{ calls int }

func (e *engine) Find(context.Context, json.RawMessage) (json.RawMessage, error) {
	e.calls++
	return json.RawMessage(`{"ok":true}`), nil
}
func event(method, path string) events.APIGatewayV2HTTPRequest {
	return events.APIGatewayV2HTTPRequest{Version: "2.0", RawPath: path, RequestContext: events.APIGatewayV2HTTPRequestContext{HTTP: events.APIGatewayV2HTTPRequestContextHTTPDescription{Method: method}}}
}
func TestActualHandlerRetainsDenyAndLiveness(t *testing.T) {
	e := &engine{}
	h, err := service.New(service.DenyAll{}, e, "fixture", 1, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	q := event("POST", "/v1/find")
	q.Body = `{"query":"secret"}`
	q.Headers = map[string]string{"Authorization": "Bearer forged", "X-Role": "admin"}
	out, err := Handle(context.Background(), h, q)
	if err != nil || out.StatusCode != http.StatusForbidden || e.calls != 0 {
		t.Fatalf("grant changed: %d %v", out.StatusCode, err)
	}
	out, err = Handle(context.Background(), h, event("GET", "/healthz"))
	if err != nil || out.StatusCode != http.StatusNoContent {
		t.Fatalf("health: %d %v", out.StatusCode, err)
	}
}
func TestBoundariesAndBinary(t *testing.T) {
	h := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.RawQuery != "x=1" || r.Header.Get("Authorization") != "Bearer test" {
			t.Error("request differs")
		}
		w.Header().Add("Set-Cookie", "a=b")
		w.WriteHeader(201)
		_, _ = w.Write([]byte{0, 255})
	})
	e := event("POST", "/v1/find")
	e.RawQueryString = "x=1"
	e.Headers = map[string]string{"Authorization": "Bearer test"}
	e.Body = base64.StdEncoding.EncodeToString([]byte{0, 255})
	e.IsBase64Encoded = true
	out, err := Handle(context.Background(), h, e)
	if err != nil || out.StatusCode != 201 || out.Body != "AP8=" || !out.IsBase64Encoded || len(out.Cookies) != 1 {
		t.Fatalf("response differs: %#v %v", out, err)
	}
	e.RawPath = "//evil"
	out, _ = Handle(context.Background(), h, e)
	if out.StatusCode != 400 {
		t.Fatal("absolute path accepted")
	}
	e.RawPath = "/x"
	e.Headers = map[string]string{"bad\nheader": "x"}
	out, _ = Handle(context.Background(), h, e)
	if out.StatusCode != 400 {
		t.Fatal("invalid header accepted")
	}
	e.Headers = nil
	e.IsBase64Encoded = false
	e.Body = strings.Repeat("x", maxBody+1)
	out, _ = Handle(context.Background(), h, e)
	if out.StatusCode != 413 {
		t.Fatal("oversized body accepted")
	}
}
func TestContextCancellationForwarded(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	var seen error
	h := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { seen = r.Context().Err() })
	_, err := Handle(ctx, h, event("GET", "/healthz"))
	if err != nil || !errors.Is(seen, context.Canceled) {
		t.Fatal("context not propagated")
	}
}

func TestAmbiguousAuthorizationAndLargeResponseRefused(t *testing.T) {
	calls := 0
	h := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { calls++; _, _ = w.Write(make([]byte, maxResponse+1)) })
	e := event("POST", "/v1/find")
	e.Headers = map[string]string{"Authorization": "Bearer one", "authorization": "Bearer two"}
	out, _ := Handle(context.Background(), h, e)
	if out.StatusCode != http.StatusBadRequest || calls != 0 {
		t.Fatal("ambiguous credentials accepted")
	}
	e.Headers = nil
	out, _ = Handle(context.Background(), h, e)
	if out.StatusCode != http.StatusBadGateway {
		t.Fatal("oversized output escaped bound")
	}
	body, _ := base64.StdEncoding.DecodeString(out.Body)
	var env map[string]string
	if json.Unmarshal(body, &env) != nil || env["error"] != "response too large" {
		t.Fatal("oversized output did not answer the JSON error envelope")
	}
}

func TestGatewayCombinedCredentialsRefused(t *testing.T) {
	h := http.HandlerFunc(func(http.ResponseWriter, *http.Request) { t.Error("ambiguous credentials reached handler") })
	e := event("POST", "/v1/find")
	e.Headers = map[string]string{"authorization": "Bearer one,Bearer two"}
	out, _ := Handle(context.Background(), h, e)
	if out.StatusCode != http.StatusBadRequest {
		t.Fatal("combined authorization accepted")
	}
	e.Headers = map[string]string{"cookie": "a=b"}
	e.Cookies = []string{"c=d"}
	out, _ = Handle(context.Background(), h, e)
	if out.StatusCode != http.StatusBadRequest {
		t.Fatal("ambiguous cookies accepted")
	}
}

// The authorizer here is a fixture only; production remains DenyAll.
type fixtureCustody struct{}

func (fixtureCustody) Claim() (func() error, error) { return func() error { return nil }, nil }

type fixtureAuth struct{}

func (fixtureAuth) Allow(_ context.Context, credential, resource, verb string) (bool, error) {
	return credential == "Bearer fixture" && resource == "index" && verb == "query", nil
}
func TestGatewayThroughHandlerAndLoopbackEngine(t *testing.T) {
	calls := 0
	engineHTTP := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Path != "/find" {
			t.Error("engine route differs")
		}
		_, _ = w.Write([]byte(`{"matches":[]}`))
	}))
	defer engineHTTP.Close()
	actualEngine, err := service.NewLocalEngine(engineHTTP.URL)
	if err != nil {
		t.Fatal(err)
	}
	h, err := service.New(fixtureAuth{}, actualEngine, "index", 1, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err := h.SetCustody(fixtureCustody{}); err != nil {
		t.Fatal(err)
	}
	e := event("POST", "/v1/find")
	e.Headers = map[string]string{"Authorization": "Bearer fixture"}
	e.Body = `{"query":"needle"}`
	out, err := Handle(context.Background(), h, e)
	body, decodeErr := base64.StdEncoding.DecodeString(out.Body)
	if err != nil || decodeErr != nil || out.StatusCode != 200 || string(body) != `{"matches":[]}` || calls != 1 {
		t.Fatalf("adapter/handler/engine path failed: status=%d calls=%d", out.StatusCode, calls)
	}
}

func TestMissingEventMethodDoesNotDispatchAsGet(t *testing.T) {
	e := &engine{}
	serviceHandler, err := service.New(service.DenyAll{}, e, "fixture", 1, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	calls := 0
	seenMethod := ""
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		seenMethod = r.Method
		serviceHandler.ServeHTTP(w, r)
	})
	out, err := Handle(context.Background(), handler, event("", "/healthz"))
	if err != nil || out.StatusCode != http.StatusBadRequest || calls != 0 {
		t.Fatalf("missing method dispatched as %q: status=%d calls=%d error=%v", seenMethod, out.StatusCode, calls, err)
	}
	body, err := base64.StdEncoding.DecodeString(out.Body)
	if err != nil {
		t.Fatal(err)
	}
	var envelope map[string]string
	if err := json.Unmarshal(body, &envelope); err != nil || envelope["error"] != "bad request" {
		t.Fatalf("invalid request envelope: %q %v", body, err)
	}
	out, err = Handle(context.Background(), handler, event(http.MethodGet, "/healthz"))
	if err != nil || out.StatusCode != http.StatusNoContent || calls != 1 || seenMethod != http.MethodGet {
		t.Fatalf("valid GET changed: status=%d calls=%d method=%q error=%v", out.StatusCode, calls, seenMethod, err)
	}
}
