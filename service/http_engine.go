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
	return &LocalEngine{strings.TrimRight(u.String(), "/"), &http.Client{Timeout: 30 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}, nil
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
			return nil, fmt.Errorf("engine rejected request: %s", detail)
		}
		return nil, fmt.Errorf("engine rejected request: %s", resp.Status)
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
