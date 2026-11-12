// Package service provides a portable, fail-closed boundary around a Resembl engine.
package service

import (
	"context"
	crypto_rand "crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math/rand"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
	"unicode/utf8"
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

// safeLogPath strips control characters (including CR/LF) from a request path
// so a crafted URL cannot forge log entries when logged via log.Printf.
func safeLogPath(p string) string {
	return strings.Map(func(ch rune) rune {
		if ch < 0x20 || (ch >= 0x7f && ch <= 0x9f) || ch == '\u2028' || ch == '\u2029' {
			return -1
		}
		return ch
	}, p)
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
	now      func() time.Time
	busy     int64
}

func (m *serviceMetrics) recordBusy() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.busy++
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
	now := m.now
	if now == nil {
		now = time.Now
	}
	fmt.Fprintf(&b, "resembl_service_uptime_seconds %.3f\n", now().Sub(m.started).Seconds())
	b.WriteString("# HELP resembl_service_busy_total Requests refused because every concurrency slot was held.\n# TYPE resembl_service_busy_total counter\n")
	fmt.Fprintf(&b, "resembl_service_busy_total %d\n", m.busy)
	b.WriteString("# HELP resembl_service_requests_total Requests answered, by path and status.\n# TYPE resembl_service_requests_total counter\n")
	requestKeys := make([][2]string, 0, len(m.requests))
	for key := range m.requests {
		requestKeys = append(requestKeys, key)
	}
	sort.Slice(requestKeys, func(i, j int) bool {
		if requestKeys[i][0] != requestKeys[j][0] {
			return requestKeys[i][0] < requestKeys[j][0]
		}
		return requestKeys[i][1] < requestKeys[j][1]
	})
	for _, key := range requestKeys {
		count := m.requests[key]
		fmt.Fprintf(&b, "resembl_service_requests_total{path=%q,status=%q} %d\n", key[0], key[1], count)
	}
	b.WriteString("# HELP resembl_service_request_duration_seconds Answered-request latency, by path.\n# TYPE resembl_service_request_duration_seconds histogram\n")
	labels := make([]string, 0, len(m.counts))
	for label := range m.counts {
		labels = append(labels, label)
	}
	sort.Strings(labels)
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
	now := time.Now
	if v, pinned := os.LookupEnv("RESEMBL_NOW"); pinned && strings.TrimSpace(v) != "" {
		if err := validateNow(v); err != nil {
			return nil, err
		}
		// Pin started to a fixed instant so uptime renders 0.000 under
		// RESEMBL_NOW, matching Python monotonic_now behaviour.
		now = m_started_fixed
	}
	// Validate RESEMBL_SEED at startup (fail-fast): an unparseable seed
	// must not silently fall back to OS randomness, matching Python's
	// sample_rng ValueError contract. Base 10 only, like Python: a
	// leading-zero decimal is valid, hex/octal spellings are not.
	if seed := os.Getenv("RESEMBL_SEED"); strings.TrimSpace(seed) != "" {
		if _, err := strconv.ParseInt(strings.TrimSpace(seed), 10, 64); err != nil {
			return nil, fmt.Errorf("RESEMBL_SEED=%q is not an integer", seed)
		}
	}
	return &Service{Authority: authority, Engine: engine, Resource: resource, slots: make(chan struct{}, concurrency), timeout: timeout, metrics: &serviceMetrics{requests: make(map[[2]string]int64), buckets: make(map[[2]interface{}]int64), sums: make(map[string]float64), counts: make(map[string]int64), started: now(), now: now}}, nil
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
func writeError(w http.ResponseWriter, status int, message string, reqID string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	// Encode writes to w; a failure means the client is gone. Log it
	// rather than discarding silently, but the status is already sent.
	if err := json.NewEncoder(w).Encode(map[string]string{"error": message}); err != nil {
		log.Printf("[%s] writeError failed: %v", reqID, err)
	}
}

var (
	seedRNGMu sync.Mutex
	seedRNG   *rand.Rand
	seedVal   int64
	seedInit  bool
)

func m_started_fixed() time.Time { return time.Date(2024, 6, 1, 12, 0, 0, 0, time.UTC) }

// normalizeOffset validates the offset on candidate and rewrites it to
// the +HH:MM form Go's RFC3339 parser requires, returning the rewritten
// candidate and whether the offset was valid. A naive candidate (no
// trailing offset) is returned unchanged and valid; the caller falls
// back to naive layouts for it. Python's fromisoformat also accepts
// hour-only (+00), colon-less (+0000, +000000) and offset-with-seconds
// (+HH:MM:SS, +HHMMSS, with optional fractional offset) spellings,
// which Go's RFC3339 layout does not, so they are normalized here
// before parsing. The instant value is never used (validateNow only
// gates RESEMBL_NOW), so dropping offset seconds after range-checking
// them changes no observable behaviour. Out-of-range offsets
// (hour > 23, minute > 59, second > 59) are rejected: Go's time.Parse
// accepts +24:00 and +23:60, which the Python parse_instant this
// mirrors rejects, so a pin would validate in one process and fail
// fast in the other.
func normalizeOffset(candidate string) (string, bool) {
	sign := byte(0)
	offsetStart := -1
	for i := 11; i < len(candidate); i++ {
		if candidate[i] == '+' || candidate[i] == '-' {
			sign = candidate[i]
			offsetStart = i
			break
		}
	}
	if offsetStart < 0 {
		return candidate, true
	}
	offset := candidate[offsetStart+1:]
	digits := ""
	if strings.Contains(offset, ":") {
		parts := strings.Split(offset, ":")
		if len(parts) < 2 || len(parts) > 3 {
			return candidate, false
		}
		for _, part := range parts {
			if part == "" {
				return candidate, false
			}
		}
		hourStr, minuteStr := parts[0], parts[1]
		if len(hourStr) != 2 || len(minuteStr) != 2 {
			return candidate, false
		}
		digits = hourStr + minuteStr
		if len(parts) == 3 {
			secondStr := parts[2]
			if dot := strings.IndexByte(secondStr, '.'); dot >= 0 {
				secondStr = secondStr[:dot]
			}
			if len(secondStr) != 2 {
				return candidate, false
			}
			digits += secondStr
		}
	} else {
		rest := offset
		if dot := strings.IndexByte(rest, '.'); dot >= 0 {
			rest = rest[:dot]
		}
		digits = rest
		switch len(digits) {
		case 2, 4, 6:
		default:
			return candidate, false
		}
	}
	for i := 0; i < len(digits); i++ {
		if digits[i] < '0' || digits[i] > '9' {
			return candidate, false
		}
	}
	hour := int(digits[0]-'0')*10 + int(digits[1]-'0')
	minute := 0
	if len(digits) >= 4 {
		minute = int(digits[2]-'0')*10 + int(digits[3]-'0')
	}
	second := 0
	if len(digits) == 6 {
		second = int(digits[4]-'0')*10 + int(digits[5]-'0')
	}
	if hour > 23 || minute > 59 || second > 59 {
		return candidate, false
	}
	if len(digits) == 2 {
		digits += "00"
	}
	return candidate[:offsetStart] + string(sign) + digits[:2] + ":" + digits[2:4], true
}

// isASCII reports whether s is all ASCII, so the byte-indexed shape gate in
// isISO8601Instant names the same characters Python's code-point gate
// names: either form rejects a non-ASCII digit or letter before any
// separator position is read.
func isASCII(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] >= 0x80 {
			return false
		}
	}
	return true
}

// isISO8601Instant mirrors resembl.models.parse_instant: the extended
// YYYY-MM-DDTHH:MM:SS shape (T/t/space separator) with optional fractional
// seconds and offset, trailing Z/z treated as +00:00. Date-only and
// minute-precision forms are not instants and return false.
//
// The shape gate pins the digit positions to ASCII: an instant's fields are
// ASCII digits, never a Unicode spelling of one. Byte-indexing alone cannot
// say so — a multi-byte digit or letter in the first 19 bytes shifts every
// later offset, and a single-byte-position check that only looks at the
// separators lets e.g. "2024-0é-01T..." through the gate. Reject anything
// non-ASCII up front; the parse below would fail on it anyway, and agreeing
// with Python's code-point gate (which counts characters, not bytes) is the
// point of the mirror.
func isISO8601Instant(s string) bool {
	if !isASCII(s) {
		return false
	}
	if len(s) < 19 || s[4] != '-' || s[7] != '-' || (s[10] != 'T' && s[10] != 't' && s[10] != ' ') {
		return false
	}
	if s[13] != ':' || s[16] != ':' {
		return false
	}
	candidate := s
	if strings.HasSuffix(candidate, "z") || strings.HasSuffix(candidate, "Z") {
		candidate = candidate[:len(candidate)-1] + "+00:00"
	}
	if len(candidate) > 10 && candidate[10] == 't' {
		candidate = candidate[:10] + "T" + candidate[11:]
	}
	source := candidate
	// Go's RFC3339 requires a colon in the offset; Python's fromisoformat
	// accepts the colon-less (+0000) and hour-only (+00) spellings, so
	// normalize them to +00:00 before parsing to keep the two validators
	// in agreement on RESEMBL_NOW.
	candidate, valid := normalizeOffset(candidate)
	if !valid {
		return false
	}
	// -00:00 (in any spelling) is ISO 8601's "unknown local offset", not
	// UTC: Go's RFC3339 parse maps it to UTC, so accepting it would
	// fabricate a UTC instant the value never stated, while the Python
	// parse_instant this mirrors rejects it. Leave it not-an-instant.
	// The exact unknown-offset spellings are rejected on the source
	// (before normalization collapses offset-with-seconds onto -00:00):
	// Python rejects exactly -00:00 / -000000 / -00:00:00 but accepts
	// -00:00:00.123 as a known small offset, so a fractional offset
	// must not be rejected by the exact-spelling check.
	for _, suffix := range []string{"-00:00", "-0000", "-00", "-00:00:00", "-000000"} {
		if strings.HasSuffix(source, suffix) {
			return false
		}
	}
	// Go requires T separator for RFC3339; normalize space before parsing.
	rfcCandidate := candidate
	if len(rfcCandidate) > 10 && rfcCandidate[10] == ' ' {
		rfcCandidate = rfcCandidate[:10] + "T" + rfcCandidate[11:]
	}
	if _, err := time.Parse(time.RFC3339Nano, rfcCandidate); err == nil {
		return true
	}
	naive := candidate
	if len(naive) > 10 && naive[10] == ' ' {
		naive = naive[:10] + "T" + naive[11:]
	}
	if _, err := time.Parse("2006-01-02T15:04:05.999999999", naive); err == nil {
		return true
	}
	if _, err := time.Parse("2006-01-02T15:04:05", naive); err == nil {
		return true
	}
	return false
}

func validateNow(v string) error {
	if !isISO8601Instant(strings.TrimSpace(v)) {
		return fmt.Errorf("RESEMBL_NOW=%q is not an ISO 8601 instant", v)
	}
	return nil
}

func newRequestID() string {
	if seed := os.Getenv("RESEMBL_SEED"); strings.TrimSpace(seed) != "" {
		v, err := strconv.ParseInt(strings.TrimSpace(seed), 10, 64)
		if err != nil {
			panic(fmt.Sprintf("RESEMBL_SEED=%q is not an integer: %v", seed, err))
		}
		seedRNGMu.Lock()
		if !seedInit || seedVal != v || seedRNG == nil {
			seedRNG = rand.New(rand.NewSource(v))
			seedVal = v
			seedInit = true
		}
		r := seedRNG
		seedRNGMu.Unlock()
		seedRNGMu.Lock()
		defer seedRNGMu.Unlock()
		b := make([]byte, 8)
		for i := range b {
			b[i] = byte(r.Intn(256))
		}
		return hex.EncodeToString(b)
	}
	b := make([]byte, 8)
	if _, err := crypto_rand.Read(b); err != nil {
		return "-"
	}
	return hex.EncodeToString(b)
}

type statusRecorder struct {
	http.ResponseWriter
	status   int
	wroteHdr bool
}

func (s *statusRecorder) WriteHeader(code int) {
	if s.wroteHdr {
		return
	}
	s.wroteHdr = true
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}

func (s *statusRecorder) Write(b []byte) (int, error) {
	if !s.wroteHdr {
		s.wroteHdr = true
		if s.status == 0 {
			s.status = http.StatusOK
		}
	}
	return s.ResponseWriter.Write(b)
}

// requestIDContextKey carries the correlation id ServeHTTP minted for a
// request, so the engine hop downstream can forward it unchanged.
type requestIDContextKey struct{}

// requestIDFromContext returns the correlation id ServeHTTP stored on
// ctx, or "" when none was. Engines that forward that id downstream
// (LocalEngine) read it here, so one id names a request at every hop.
func requestIDFromContext(ctx context.Context) string {
	if v, ok := ctx.Value(requestIDContextKey{}).(string); ok {
		return v
	}
	return ""
}

func (s *Service) now() time.Time {
	if s.metrics != nil && s.metrics.now != nil {
		return s.metrics.now()
	}
	return time.Now()
}

func (s *Service) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	reqID := newRequestID()
	// Adopt the caller's correlation id the same way the Python engine
	// does (_correlation_id in resembl/server.py): one id then names the
	// request in the Go service's logs, in the engine's logs, and on the
	// response the client gets back, so an operator holding a client
	// report can search every hop for it. The header is request-controlled,
	// so only a bounded, printable id is adopted; anything else (too long,
	// or carrying a control char that could forge a log line) falls back
	// to the server-minted id.
	if supplied := strings.TrimSpace(r.Header.Get("X-Request-Id")); supplied != "" && len(supplied) <= 64 && safeLogPath(supplied) == supplied {
		reqID = supplied
	}
	started := s.now()
	// Carry the id on the request context so the engine hop downstream
	// can forward it (LocalEngine sets X-Request-Id): one id then names
	// this request in the service's logs, in the engine's logs, and on
	// the response the client gets back.
	r = r.WithContext(context.WithValue(r.Context(), requestIDContextKey{}, reqID))
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Request-Id", reqID)
	rec := &statusRecorder{ResponseWriter: w}
	defer func() {
		status := rec.status
		if status == 0 {
			status = http.StatusOK
		}
		elapsed := s.now().Sub(started)
		if s.metrics != nil {
			s.metrics.record(r.URL.Path, status, elapsed.Seconds())
		}
		if status >= 500 {
			log.Printf("[%s] %s %s -> %d in %v", reqID, safeLogPath(r.Method), safeLogPath(r.URL.Path), status, elapsed)
		} else if elapsed >= time.Second {
			log.Printf("[%s] slow %s %s -> %d in %v", reqID, safeLogPath(r.Method), safeLogPath(r.URL.Path), status, elapsed)
		}
	}()
	w = rec
	// Inbound body lifecycle (resource review): handlers that answer before
	// consuming the body (health/metrics/method refusals, authority and
	// custody failures, busy slots) must drain and close it so the transport
	// can reuse the connection. The stdlib does not do this for us; an
	// unread body pins the connection until idle timeout. A single deferred
	// drain (bounded) + close covers every exit path, including panic.
	defer func() {
		if r.Body != nil {
			_, _ = io.Copy(io.Discard, io.LimitReader(r.Body, 8<<20))
			_ = r.Body.Close()
		}
	}()
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("X-Frame-Options", "DENY")
	w.Header().Set("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
	w.Header().Set("Referrer-Policy", "no-referrer")
	w.Header().Set("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
	w.Header().Set("Cross-Origin-Resource-Policy", "same-origin")
	if r.URL.Path == "/healthz" {
		if r.Method != http.MethodGet {
			w.Header().Set("Allow", "GET")
			writeError(w, http.StatusMethodNotAllowed, "method not allowed", reqID)
			return
		}
		w.WriteHeader(http.StatusNoContent)
		return
	}
	if r.URL.Path == "/metrics" {
		if r.Method != http.MethodGet {
			w.Header().Set("Allow", "GET")
			writeError(w, http.StatusMethodNotAllowed, "method not allowed", reqID)
			return
		}
		// Metrics expose request counts, latency and error rates for the
		// corpus service; they are not liveness. Require the same query
		// authorization as /v1/find so an unauthenticated caller cannot
		// read operational data from a token-protected deployment.
		// /healthz stays unauthenticated for orchestrator probes.
		metricsCtx, metricsCancel := context.WithTimeout(r.Context(), s.timeout)
		defer metricsCancel()
		allowed, err := s.Authority.Allow(metricsCtx, r.Header.Get("Authorization"), s.Resource, "query")
		if err != nil {
			log.Printf("[%s] authority unavailable: %v", reqID, err)
			writeError(w, http.StatusServiceUnavailable, "authority unavailable", reqID)
			return
		}
		if !allowed {
			log.Printf("[%s] %s %s forbidden", reqID, safeLogPath(r.Method), safeLogPath(r.URL.Path))
			writeError(w, http.StatusForbidden, "forbidden", reqID)
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
		safe := strings.Map(func(ch rune) rune {
			if ch < 0x20 || ch == 0x7f {
				return -1
			}
			return ch
		}, r.URL.Path)
		if safe == "" {
			safe = r.URL.Path
		}
		writeError(w, http.StatusNotFound, "unknown path: "+safe, reqID)
		return
	}
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", "POST")
		writeError(w, http.StatusMethodNotAllowed, "method not allowed", reqID)
		return
	}
	if ct := r.Header.Get("Content-Type"); ct != "" {
		media := strings.TrimSpace(strings.SplitN(ct, ";", 2)[0])
		// ASCII-only fold: EqualFold is the Unicode simple fold, so a
		// non-ASCII spelling that folds into ASCII (long s U+017F -> s,
		// Kelvin sign U+212A -> k) would pass as "application/json" here
		// while the Python engine's casefold comparison admits the same
		// spellings for a different reason — both must fold protocol
		// tokens ASCII a-z only, like log_level_get does.
		if !isASCII(media) || !strings.EqualFold(media, "application/json") {
			writeError(w, http.StatusUnsupportedMediaType, "content type must be application/json", reqID)
			return
		}
	}
	ctx, cancel := context.WithTimeout(r.Context(), s.timeout)
	defer cancel()
	allowed, err := s.Authority.Allow(ctx, r.Header.Get("Authorization"), s.Resource, "query")
	if err != nil {
		log.Printf("[%s] authority unavailable: %v", reqID, err)
		writeError(w, http.StatusServiceUnavailable, "authority unavailable", reqID)
		return
	}
	if !allowed {
		log.Printf("[%s] %s %s forbidden", reqID, safeLogPath(r.Method), safeLogPath(r.URL.Path))
		writeError(w, http.StatusForbidden, "forbidden", reqID)
		return
	}
	custody := s.getCustody()
	if custody == nil {
		log.Printf("[%s] custody unavailable", reqID)
		writeError(w, http.StatusServiceUnavailable, "custody unavailable", reqID)
		return
	}
	dispatched, completed := false, false
	select {
	case s.slots <- struct{}{}:
		defer func() {
			// A completed response proves compute stopped even after cancellation.
			if completed || !dispatched || ctx.Err() == nil {
				<-s.slots
			}
		}()
	default:
		if s.metrics != nil {
			s.metrics.recordBusy()
		}
		// Overload is a transient-capacity signal, not a dead service, so
		// answer 429 and tell the caller when to retry instead of the
		// 503 the unavailable-engine paths use; nothing was dispatched
		// for this request, so a retry against a later slot is safe.
		log.Printf("[%s] busy: concurrency slots exhausted", reqID)
		w.Header().Set("Retry-After", "1")
		writeError(w, http.StatusTooManyRequests, "busy", reqID)
		return
	}
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 8<<20))
	if err != nil {
		if ctx.Err() != nil {
			log.Printf("[%s] request timed out", reqID)
			writeError(w, http.StatusServiceUnavailable, "request timed out", reqID)
			return
		}
		if errors.Is(err, http.ErrBodyReadAfterClose) || strings.Contains(err.Error(), "request body too large") {
			writeError(w, http.StatusRequestEntityTooLarge, "request too large", reqID)
			return
		}
		writeError(w, http.StatusBadRequest, "request body unreadable", reqID)
		return
	}
	var body map[string]json.RawMessage
	if !utf8.Valid(raw) || json.Unmarshal(raw, &body) != nil || body == nil {
		writeError(w, http.StatusBadRequest, "bad request body", reqID)
		return
	}
	var result json.RawMessage
	var release func() error
	if custody != nil {
		release, err = custody.Claim()
		if err != nil {
			log.Printf("[%s] capacity unavailable: %v", reqID, err)
			writeError(w, http.StatusServiceUnavailable, "capacity unavailable", reqID)
			return
		}
	}
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
		if _, present := body["query"]; !present {
			writeError(w, http.StatusBadRequest, "bad request: 'query' is required", reqID)
			return
		}
		// A JSON null unmarshals into a Go string as "" without error,
		// which would answer "must not be empty" instead of the Python
		// server's "query must be a string" for the same body.
		if string(body["query"]) == "null" {
			writeError(w, http.StatusBadRequest, "bad request: query must be a string", reqID)
			return
		}
		if json.Unmarshal(body["query"], &query) != nil {
			writeError(w, http.StatusBadRequest, "bad request: query must be a string", reqID)
			return
		}
		if strings.TrimSpace(query) == "" {
			writeError(w, http.StatusBadRequest, "bad request: query must not be empty", reqID)
			return
		}
		dispatched = true
		result, err = s.Engine.Find(ctx, raw)
	} else {
		batch, ok := s.Engine.(BatchEngine)
		if !ok {
			writeError(w, http.StatusNotImplemented, "batch unavailable", reqID)
			return
		}
		var queries []json.RawMessage
		if _, present := body["queries"]; !present {
			writeError(w, http.StatusBadRequest, "bad request: 'queries' is required", reqID)
			return
		}
		if json.Unmarshal(body["queries"], &queries) != nil || queries == nil || len(queries) > 1000 {
			if queries != nil && len(queries) > 1000 {
				writeError(w, http.StatusBadRequest, "queries must hold at most 1000 entries", reqID)
				return
			}
			writeError(w, http.StatusBadRequest, "bad request: queries must be a list", reqID)
			return
		}
		dispatched = true
		result, err = batch.FindBatch(ctx, raw)
	}
	if err != nil {
		var engErr *EngineError
		if errors.As(err, &engErr) {
			// The warm engine returns these only for validation failures.
			completed = engErr.Status == http.StatusBadRequest || engErr.Status == http.StatusRequestEntityTooLarge
			if engErr.Status >= 400 && engErr.Status < 500 {
				// A client-caused rejection from the warm engine (bad
				// parameter, index mismatch) is the caller's 4xx,
				// relayed with its envelope text instead of a 503
				// that claims the engine is down.
				msg := engErr.Message
				if msg == "" {
					msg = "engine rejected request"
				}
				log.Printf("[%s] engine rejected request: status=%d", reqID, engErr.Status)
				writeError(w, engErr.Status, msg, reqID)
				return
			}
			log.Printf("[%s] engine unavailable: %v", reqID, err)
			writeError(w, http.StatusServiceUnavailable, "engine unavailable", reqID)
			return
		}
		log.Printf("[%s] engine unavailable: %v", reqID, err)
		writeError(w, http.StatusServiceUnavailable, "engine unavailable", reqID)
		return
	}
	completed = true
	if !utf8.Valid(result) || !json.Valid(result) {
		log.Printf("[%s] invalid engine response", reqID)
		writeError(w, http.StatusBadGateway, "invalid engine response", reqID)
		return
	}
	// Release now, while the status can still be changed: a failed release
	// leaves durable capacity unaccounted, so the answer must say so.
	if release != nil {
		err := release()
		release = nil
		if err != nil {
			log.Printf("[%s] capacity release failed: %v", reqID, err)
			writeError(w, http.StatusServiceUnavailable, "capacity release failed", reqID)
			return
		}
	}
	w.Header().Set("Content-Type", "application/json")
	if _, err := w.Write(result); err != nil {
		log.Printf("[%s] response write failed: %v", reqID, err)
		// Response may be truncated on the wire; the claim is already
		// released, so the client must retry against fresh capacity.
		// Headers/status are already committed by the partial write;
		// a second WriteHeader would be a superfluous log line, not a
		// recoverable status change. The error is logged above.
		return
	}
}
