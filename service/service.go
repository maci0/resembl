// Package service provides a portable, fail-closed boundary around a Resembl engine.
package service

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// Authorizer authenticates the caller and checks a server-selected resource and verb.
// Implementations must consult current native authority and fail closed on uncertainty.
type Authorizer interface {
	Allow(context.Context, string, string, string) (bool, error)
}

// Engine invokes a qualified pinned Resembl instance; it must honor cancellation.
type Engine interface {
	Find(context.Context, json.RawMessage) (json.RawMessage, error)
}

type BatchEngine interface {
	FindBatch(context.Context, json.RawMessage) (json.RawMessage, error)
}

type DenyAll struct{}

func (DenyAll) Allow(context.Context, string, string, string) (bool, error) { return false, nil }

type Service struct {
	Authority Authorizer
	Engine    Engine
	Resource  string
	slots     chan struct{}
	timeout   time.Duration
	custody   atomic.Pointer[custodyHolder]
	metrics   *serviceMetrics
}

type custodyHolder struct {
	custody Custody
}

// latency buckets mirror the Python server's histogram bounds so both
// exposures answer the same latency questions.
var latencyBuckets = []float64{0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 30.0}

// serviceMetrics holds in-process counters exposed at GET /metrics.
// No user-controlled label values: cardinality is bounded by fixed paths
// and status codes, mirroring the Python server.
type serviceMetrics struct {
	mu       sync.Mutex
	requests map[[2]string]int64
	buckets  map[[2]interface{}]int64
	sums     map[string]float64
	counts   map[string]int64
	started  time.Time
}

func (m *serviceMetrics) record(path string, status int, seconds float64) {
	label := path
	if label != "/v1/find" && label != "/v1/find-batch" && label != "/healthz" && label != "/metrics" {
		label = "other"
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.requests[[2]string{label, strconv.Itoa(status)}]++
	m.sums[label] += seconds
	m.counts[label]++
	for _, bound := range latencyBuckets {
		if seconds <= bound {
			m.buckets[[2]interface{}{label, bound}]++
		}
	}
}

func (m *serviceMetrics) render() string {
	m.mu.Lock()
	defer m.mu.Unlock()
	var b strings.Builder
	b.WriteString("# HELP resembl_service_up 1 while this process is answering requests.\n# TYPE resembl_service_up gauge\nresembl_service_up 1\n")
	b.WriteString("# HELP resembl_service_uptime_seconds Seconds since this process started serving.\n# TYPE resembl_service_uptime_seconds gauge\n")
	fmt.Fprintf(&b, "resembl_service_uptime_seconds %.3f\n", time.Since(m.started).Seconds())
	b.WriteString("# HELP resembl_service_requests_total Requests answered, by path and status.\n# TYPE resembl_service_requests_total counter\n")
	for key, count := range m.requests {
		fmt.Fprintf(&b, "resembl_service_requests_total{path=%q,status=%q} %d\n", key[0], key[1], count)
	}
	b.WriteString("# HELP resembl_service_request_duration_seconds Answered-request latency, by path.\n# TYPE resembl_service_request_duration_seconds histogram\n")
	labels := make([]string, 0, len(m.counts))
	for label := range m.counts {
		labels = append(labels, label)
	}
	// deterministic order for stable exposition
	for i := 0; i < len(labels); i++ {
		for j := i + 1; j < len(labels); j++ {
			if labels[j] < labels[i] {
				labels[i], labels[j] = labels[j], labels[i]
			}
		}
	}
	for _, label := range labels {
		for _, bound := range latencyBuckets {
			fmt.Fprintf(&b, "resembl_service_request_duration_seconds_bucket{path=%q,le=%q} %d\n", label, strconv.FormatFloat(bound, 'f', -1, 64), m.buckets[[2]interface{}{label, bound}])
		}
		fmt.Fprintf(&b, "resembl_service_request_duration_seconds_bucket{path=%q,le=%q} %d\n", label, "+Inf", m.counts[label])
		fmt.Fprintf(&b, "resembl_service_request_duration_seconds_sum{path=%q} %.6f\n", label, m.sums[label])
		fmt.Fprintf(&b, "resembl_service_request_duration_seconds_count{path=%q} %d\n", label, m.counts[label])
	}
	return b.String()
}

func New(authority Authorizer, engine Engine, resource string, concurrency int, timeout time.Duration) (*Service, error) {
	if authority == nil || engine == nil || resource == "" || concurrency < 1 || concurrency > 64 || timeout <= 0 {
		return nil, errors.New("invalid service configuration")
	}
	return &Service{Authority: authority, Engine: engine, Resource: resource, slots: make(chan struct{}, concurrency), timeout: timeout, metrics: &serviceMetrics{requests: make(map[[2]string]int64), buckets: make(map[[2]interface{}]int64), sums: make(map[string]float64), counts: make(map[string]int64), started: time.Now()}}, nil
}

// SetCustody installs an owner-supplied durable capacity store before serving.
func (s *Service) SetCustody(c Custody) error {
	if c == nil {
		return errors.New("custody required")
	}
	s.custody.Store(&custodyHolder{custody: c})
	return nil
}

func (s *Service) getCustody() Custody {
	if h := s.custody.Load(); h != nil {
		return h.custody
	}
	return nil
}
func writeError(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": message})
}

func newRequestID() string {
	b := make([]byte, 8)
	if _, err := rand.Read(b); err != nil {
		return "-"
	}
	return hex.EncodeToString(b)
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (s *statusRecorder) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}

func (s *statusRecorder) Write(b []byte) (int, error) {
	if s.status == 0 {
		s.status = http.StatusOK
	}
	return s.ResponseWriter.Write(b)
}

func (s *Service) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	reqID := newRequestID()
	started := time.Now()
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Request-Id", reqID)
	rec := &statusRecorder{ResponseWriter: w}
	defer func() {
		status := rec.status
		if status == 0 {
			status = http.StatusOK
		}
		elapsed := time.Since(started)
		if s.metrics != nil {
			s.metrics.record(r.URL.Path, status, elapsed.Seconds())
		}
		if status >= 500 {
			log.Printf("[%s] %s %s -> %d in %v", reqID, r.Method, r.URL.Path, status, elapsed)
		} else if elapsed >= time.Second {
			log.Printf("[%s] slow %s %s -> %d in %v", reqID, r.Method, r.URL.Path, status, elapsed)
		}
	}()
	w = rec
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("X-Frame-Options", "DENY")
	w.Header().Set("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
	w.Header().Set("Referrer-Policy", "no-referrer")
	w.Header().Set("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
	w.Header().Set("Cross-Origin-Resource-Policy", "same-origin")
	if r.URL.Path == "/healthz" {
		if r.Method != http.MethodGet {
			w.Header().Set("Allow", "GET")
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		w.WriteHeader(http.StatusNoContent)
		return
	}
	if r.URL.Path == "/metrics" {
		if r.Method != http.MethodGet {
			w.Header().Set("Allow", "GET")
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
		if s.metrics != nil {
			_, _ = w.Write([]byte(s.metrics.render()))
		} else {
			_, _ = w.Write([]byte("resembl_service_up 1\n"))
		}
		return
	}
	if r.URL.Path != "/v1/find" && r.URL.Path != "/v1/find-batch" {
		writeError(w, http.StatusNotFound, "not found")
		return
	}
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", "POST")
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	if ct := r.Header.Get("Content-Type"); ct != "" {
		media := strings.TrimSpace(strings.SplitN(ct, ";", 2)[0])
		if !strings.EqualFold(media, "application/json") {
			writeError(w, http.StatusUnsupportedMediaType, "content type must be application/json")
			return
		}
	}
	ctx, cancel := context.WithTimeout(r.Context(), s.timeout)
	defer cancel()
	allowed, err := s.Authority.Allow(ctx, r.Header.Get("Authorization"), s.Resource, "query")
	if err != nil {
		log.Printf("[%s] authority unavailable: %v", reqID, err)
		writeError(w, http.StatusServiceUnavailable, "authority unavailable")
		return
	}
	if !allowed {
		writeError(w, http.StatusForbidden, "forbidden")
		return
	}
	custody := s.getCustody()
	if custody == nil {
		log.Printf("[%s] custody unavailable", reqID)
		writeError(w, http.StatusServiceUnavailable, "custody unavailable")
		return
	}
	dispatched := false
	select {
	case s.slots <- struct{}{}:
		defer func() {
			// A canceled transport cannot prove remote compute stopped.
			if !dispatched || ctx.Err() == nil {
				<-s.slots
			}
		}()
	default:
		log.Printf("[%s] busy: concurrency slots exhausted", reqID)
		writeError(w, http.StatusServiceUnavailable, "busy")
		return
	}
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 8<<20))
	if err != nil {
		if ctx.Err() != nil {
			log.Printf("[%s] request timed out", reqID)
			writeError(w, http.StatusServiceUnavailable, "request timed out")
			return
		}
		if errors.Is(err, http.ErrBodyReadAfterClose) || strings.Contains(err.Error(), "request body too large") {
			writeError(w, http.StatusRequestEntityTooLarge, "request too large")
			return
		}
		writeError(w, http.StatusBadRequest, "request body unreadable")
		return
	}
	var body map[string]json.RawMessage
	if json.Unmarshal(raw, &body) != nil || body == nil {
		writeError(w, http.StatusBadRequest, "invalid request")
		return
	}
	var result json.RawMessage
	var release func() error
	if custody != nil {
		release, err = custody.Claim()
		if err != nil {
			log.Printf("[%s] capacity unavailable: %v", reqID, err)
			writeError(w, http.StatusServiceUnavailable, "capacity unavailable")
			return
		}
	}
	completed := false
	defer func() {
		if (completed || !dispatched) && release != nil {
			// Error paths only: the success path releases inline below so
			// its failure still changes the status. Here the response is
			// already decided; a failed release is logged, not swallowed.
			if err := release(); err != nil {
				log.Printf("[%s] capacity release failed: %v", reqID, err)
			}
		}
	}()
	if r.URL.Path == "/v1/find" {
		var query string
		if json.Unmarshal(body["query"], &query) != nil || strings.TrimSpace(query) == "" {
			writeError(w, http.StatusBadRequest, "query required")
			return
		}
		dispatched = true
		result, err = s.Engine.Find(ctx, raw)
	} else {
		batch, ok := s.Engine.(BatchEngine)
		if !ok {
			writeError(w, http.StatusNotImplemented, "batch unavailable")
			return
		}
		var queries []json.RawMessage
		if json.Unmarshal(body["queries"], &queries) != nil || queries == nil || len(queries) > 1000 {
			writeError(w, http.StatusBadRequest, "invalid queries")
			return
		}
		dispatched = true
		result, err = batch.FindBatch(ctx, raw)
	}
	if err != nil {
		log.Printf("[%s] engine unavailable: %v", reqID, err)
		writeError(w, http.StatusServiceUnavailable, "engine unavailable")
		return
	}
	completed = true
	if !json.Valid(result) {
		log.Printf("[%s] invalid engine response", reqID)
		writeError(w, http.StatusBadGateway, "invalid engine response")
		return
	}
	// Release now, while the status can still be changed: a failed release
	// leaves durable capacity unaccounted, so the answer must say so.
	if release != nil {
		err := release()
		release = nil
		if err != nil {
			log.Printf("[%s] capacity release failed: %v", reqID, err)
			writeError(w, http.StatusServiceUnavailable, "capacity release failed")
			return
		}
	}
	w.Header().Set("Content-Type", "application/json")
	if _, err := w.Write(result); err != nil {
		log.Printf("[%s] response write failed: %v", reqID, err)
		// Response may be truncated on the wire; the claim is already
		// released, so the client must retry against fresh capacity.
		writeError(w, http.StatusServiceUnavailable, "response write failed")
		return
	}
}
