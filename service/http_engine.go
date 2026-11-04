package service

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// LocalEngine talks to an already provisioned local warm engine. This adapter
// is optional; other runtimes supply Engine without a process or socket.
type LocalEngine struct {
	endpoint string
	client   *http.Client
	// transport is kept so CloseIdleConnections can retire pooled
	// connections when the engine is discarded.
	transport *http.Transport
}

// EngineError is a non-2xx answer from the warm engine. Status is its HTTP
// status and Message its JSON error envelope text, so Service can relay a
// client-caused rejection (400/409/...) instead of masking every rejection
// as 503 "engine unavailable".
type EngineError struct {
	Status  int
	Message string
}

func (e *EngineError) Error() string {
	if e.Message != "" {
		return fmt.Sprintf("engine rejected request: %s", e.Message)
	}
	return fmt.Sprintf("engine rejected request: status %d", e.Status)
}

func NewLocalEngine(endpoint string) (*LocalEngine, error) {
	u, err := url.Parse(endpoint)
	if err != nil || u.Scheme != "http" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") {
		return nil, errors.New("invalid engine endpoint")
	}
	ip := net.ParseIP(u.Hostname())
	if u.Hostname() != "localhost" && (ip == nil || !ip.IsLoopback()) {
		return nil, errors.New("engine must be loopback")
	}
	// Bounded transport: the default http.Transport pools up to 100 idle
	// connections per host with no idle timeout, so a long-lived service
	// whose engine is slow to answer accumulates half-dead idle sockets
	// that fail on next use.  Cap idle per host and time them out; the
	// request timeout still bounds an in-flight call.
	transport := &http.Transport{
		MaxIdleConns:        16,
		MaxIdleConnsPerHost: 8,
		IdleConnTimeout:     30 * time.Second,
	}
	return &LocalEngine{
		endpoint:  strings.TrimRight(u.String(), "/"),
		client:    &http.Client{Timeout: 30 * time.Second, Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }},
		transport: transport,
	}, nil
}

// CloseIdleConnections retires pooled connections held by this engine.
// Callers that discard an engine (shutdown, tests) should call it so the
// underlying sockets are released instead of held until the transport is
// collected.
func (e *LocalEngine) CloseIdleConnections() {
	if e.transport != nil {
		e.transport.CloseIdleConnections()
	}
}
func (e *LocalEngine) Find(ctx context.Context, body json.RawMessage) (json.RawMessage, error) {
	return e.find(ctx, "/find", body)
}
func (e *LocalEngine) FindBatch(ctx context.Context, body json.RawMessage) (json.RawMessage, error) {
	return e.find(ctx, "/find-batch", body)
}
func (e *LocalEngine) find(ctx context.Context, path string, body json.RawMessage) (json.RawMessage, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, e.endpoint+path, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := e.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		// Consume the error body (discarding it) so the connection is
		// reusable. Surface its detail: callers and operators relay
		// this error, and a generic message hides bad-request reasons
		// the engine already sent.
		data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		var detail string
		var probe struct {
			Error string `json:"error"`
		}
		if json.Unmarshal(data, &probe) == nil && probe.Error != "" {
			detail = probe.Error
		} else if len(data) > 0 {
			detail = strings.TrimSpace(string(data))
		}
		if detail != "" {
			return nil, &EngineError{Status: resp.StatusCode, Message: detail}
		}
		return nil, &EngineError{Status: resp.StatusCode, Message: resp.Status}
	}
	// Read up to one byte past the cap so an oversized body is detected
	// without allocating unboundedly, then discard any remainder so the
	// connection can be reused.
	data, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20+1))
	if err == nil && len(data) > 8<<20 {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))
		err = errors.New("invalid engine response")
	}
	if err != nil {
		return nil, err
	}
	return data, nil
}
