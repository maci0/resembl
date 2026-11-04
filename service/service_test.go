package service

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
)

type authFunc func(context.Context, string, string, string) (bool, error)

func (f authFunc) Allow(c context.Context, a, r, v string) (bool, error) { return f(c, a, r, v) }

type testCustody struct{}

func (testCustody) Claim() (func() error, error) { return func() error { return nil }, nil }
func TestBoundaryAndLocalAdapter(t *testing.T) {
	calls := 0
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Path != "/find" {
			t.Errorf("path %s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"lsh_candidates":1,"matches":[]}`))
	}))
	defer backend.Close()
	engine, err := NewLocalEngine(backend.URL)
	if err != nil {
		t.Fatal(err)
	}
	auth := authFunc(func(_ context.Context, token, resource, verb string) (bool, error) {
		if token == "unavailable" {
			return false, errors.New("down")
		}
		return token == "Bearer valid" && resource == "tenant/project/index" && verb == "query", nil
	})
	s, err := New(auth, engine, "tenant/project/index", 1, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err = s.SetCustody(testCustody{}); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		token, body string
		want        int
	}{{"", `{"query":"ret"}`, 403}, {"unavailable", `{"query":"ret"}`, 503}, {"Bearer valid", `{"query":" "}`, 400}, {"Bearer valid", `{"query":"ret"}`, 200}} {
		req := httptest.NewRequest("POST", "/v1/find", strings.NewReader(tc.body))
		req.Header.Set("Authorization", tc.token)
		w := httptest.NewRecorder()
		s.ServeHTTP(w, req)
		if w.Code != tc.want {
			t.Fatalf("got %d want %d", w.Code, tc.want)
		}
		if tc.want == 200 && !json.Valid(w.Body.Bytes()) {
			t.Fatal("invalid result")
		}
	}
	if calls != 1 {
		t.Fatalf("backend calls %d", calls)
	}
	if _, err = NewLocalEngine("https://example.test"); err == nil {
		t.Fatal("accepted remote engine")
	}
}

func TestActualWarmEngine(t *testing.T) {
	endpoint := os.Getenv("RESEMBL_TEST_ENGINE_URL")
	if endpoint == "" {
		t.Skip("set RESEMBL_TEST_ENGINE_URL to a disposable warm engine")
	}
	engine, err := NewLocalEngine(endpoint)
	if err != nil {
		t.Fatal(err)
	}
	result, err := engine.Find(context.Background(), json.RawMessage(`{"query":"mov eax, 1; ret"}`))
	if err != nil {
		t.Fatal(err)
	}
	var body struct {
		Matches []json.RawMessage `json:"matches"`
	}
	if json.Unmarshal(result, &body) != nil || body.Matches == nil {
		t.Fatalf("unexpected real engine response: %s", result)
	}
	batch, err := engine.FindBatch(context.Background(), json.RawMessage(`{"queries":["mov eax, 1; ret"]}`))
	if err != nil {
		t.Fatal(err)
	}
	var grouped struct {
		Results []json.RawMessage `json:"results"`
	}
	if json.Unmarshal(batch, &grouped) != nil || len(grouped.Results) != 1 {
		t.Fatalf("unexpected batch response: %s", batch)
	}
}

type timedEngine struct{}

func (timedEngine) Find(ctx context.Context, _ json.RawMessage) (json.RawMessage, error) {
	<-ctx.Done()
	return nil, ctx.Err()
}
func TestUncertainTimeoutQuarantinesCapacity(t *testing.T) {
	s, err := New(authFunc(func(context.Context, string, string, string) (bool, error) { return true, nil }), timedEngine{}, "index", 1, 10*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	if err = s.SetCustody(testCustody{}); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		w := httptest.NewRecorder()
		s.ServeHTTP(w, httptest.NewRequest("POST", "/v1/find", strings.NewReader(`{"query":"ret"}`)))
		if w.Code != 503 {
			t.Fatalf("request %d: %d", i, w.Code)
		}
	}
	if len(s.slots) != 1 {
		t.Fatal("uncertain execution released capacity")
	}
}

func TestPinnedAdapterRequiresExactSnapshot(t *testing.T) {
	pin := strings.Repeat("a", 64)
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/find-pinned" {
			t.Errorf("wrong path %s", r.URL.Path)
		}
		var body map[string]any
		if json.NewDecoder(r.Body).Decode(&body) != nil || body["index_sha256"] != pin {
			t.Error("missing pin")
		}
		_, _ = w.Write([]byte(`{"index_sha256":"` + pin + `","artifact_sha256":"` + pin + `","generation":"generation","result":{"matches":[]}}`))
	}))
	defer backend.Close()
	engine, err := NewPinnedSQLiteEngine(backend.URL, pin, pin, "generation")
	if err != nil {
		t.Fatal(err)
	}
	result, err := engine.Find(context.Background(), json.RawMessage(`{"query":"ret"}`))
	if err != nil || !json.Valid(result) {
		t.Fatalf("%s %v", result, err)
	}
	if _, err = NewPinnedSQLiteEngine(backend.URL, "bad", pin, "generation"); err == nil {
		t.Fatal("bad pin accepted")
	}
}

func TestRealPinnedEngineWithAuthorityBoundary(t *testing.T) {
	endpoint, index, artifact, generation := os.Getenv("RESEMBL_TEST_ENGINE_URL"), os.Getenv("RESEMBL_TEST_INDEX"), os.Getenv("RESEMBL_TEST_ARTIFACT"), os.Getenv("RESEMBL_TEST_GENERATION")
	if endpoint == "" || index == "" || artifact == "" || generation == "" {
		t.Skip("real pinned engine fixture not configured")
	}
	engine, err := NewPinnedSQLiteEngine(endpoint, index, artifact, generation)
	if err != nil {
		t.Fatal(err)
	}
	s, err := New(authFunc(func(context.Context, string, string, string) (bool, error) { return true, nil }), engine, "fixture", 1, 30*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err = s.SetCustody(testCustody{}); err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest("POST", "/v1/find", strings.NewReader(`{"query":"ret"}`))
	w := httptest.NewRecorder()
	s.ServeHTTP(w, req)
	if w.Code != 200 || !json.Valid(w.Body.Bytes()) {
		t.Fatalf("real pinned query: %d %s", w.Code, w.Body.String())
	}
	stale, err := NewPinnedSQLiteEngine(endpoint, index, artifact, "stale")
	if err != nil {
		t.Fatal(err)
	}
	if _, err = stale.Find(context.Background(), json.RawMessage(`{"query":"ret"}`)); err == nil {
		t.Fatal("stale generation accepted")
	}
	wrong, err := NewPinnedSQLiteEngine(endpoint, strings.Repeat("0", 64), artifact, generation)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = wrong.Find(context.Background(), json.RawMessage(`{"query":"ret"}`)); err == nil {
		t.Fatal("wrong index accepted")
	}
}

func TestCancelledBeforeDispatchReleasesInMemorySlot(t *testing.T) {
	s, err := New(authFunc(func(context.Context, string, string, string) (bool, error) { return true, nil }), timedEngine{}, "index", 1, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err = s.SetCustody(testCustody{}); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	req := httptest.NewRequest("POST", "/v1/find", strings.NewReader(`{}`)).WithContext(ctx)
	s.ServeHTTP(httptest.NewRecorder(), req)
	if len(s.slots) != 0 {
		t.Fatal("undispatched request retained in-memory capacity")
	}
}
