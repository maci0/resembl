// Package service provides a portable, fail-closed boundary around a Resembl engine.
package service

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"strings"
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
}

type custodyHolder struct {
	custody Custody
}

func New(authority Authorizer, engine Engine, resource string, concurrency int, timeout time.Duration) (*Service, error) {
	if authority == nil || engine == nil || resource == "" || concurrency < 1 || concurrency > 64 || timeout <= 0 {
		return nil, errors.New("invalid service configuration")
	}
	return &Service{Authority: authority, Engine: engine, Resource: resource, slots: make(chan struct{}, concurrency), timeout: timeout}, nil
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

func (s *Service) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
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
		if r.URL.RawQuery != "" {
			writeError(w, http.StatusNotFound, "not found")
			return
		}
		w.WriteHeader(http.StatusNoContent)
		return
	}
	if (r.URL.Path != "/v1/find" && r.URL.Path != "/v1/find-batch") || r.URL.RawQuery != "" {
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
		writeError(w, http.StatusServiceUnavailable, "authority unavailable")
		return
	}
	if !allowed {
		writeError(w, http.StatusForbidden, "forbidden")
		return
	}
	custody := s.getCustody()
	if custody == nil {
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
		writeError(w, http.StatusServiceUnavailable, "busy")
		return
	}
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 8<<20))
	if err != nil {
		if ctx.Err() != nil {
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
				log.Printf("capacity release failed: %v", err)
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
		writeError(w, http.StatusServiceUnavailable, "engine unavailable")
		return
	}
	completed = true
	if !json.Valid(result) {
		writeError(w, http.StatusBadGateway, "invalid engine response")
		return
	}
	// Release now, while the status can still be changed: a failed release
	// leaves durable capacity unaccounted, so the answer must say so.
	if release != nil {
		err := release()
		release = nil
		if err != nil {
			writeError(w, http.StatusServiceUnavailable, "capacity release failed")
			return
		}
	}
	w.Header().Set("Content-Type", "application/json")
	if _, err := w.Write(result); err != nil {
		// Response may be truncated on the wire; the claim is already
		// released, so the client must retry against fresh capacity.
		writeError(w, http.StatusServiceUnavailable, "response write failed")
		return
	}
}
