// Package lambdaproxy adapts API Gateway HTTP API v2 events to an existing handler.
package lambdaproxy

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"

	"github.com/aws/aws-lambda-go/events"
)

const (
	maxBody     = 8 << 20
	maxResponse = 4 << 20
)

type response struct {
	header   http.Header
	status   int
	body     bytes.Buffer
	overflow bool
}

func (r *response) Header() http.Header { return r.header }
func (r *response) WriteHeader(status int) {
	if r.status == 0 {
		r.status = status
	}
}
func (r *response) Write(p []byte) (int, error) {
	if r.status == 0 {
		r.status = http.StatusOK
	}
	if len(p) > maxResponse-r.body.Len() {
		r.overflow = true
		return 0, errors.New("response too large")
	}
	return r.body.Write(p)
}

// Handle preserves the handler's authorization and request cancellation. It never
// treats API Gateway identity claims as a grant and does not establish durable stop.
func Handle(ctx context.Context, h http.Handler, event events.APIGatewayV2HTTPRequest) (events.APIGatewayV2HTTPResponse, error) {
	isHead := event.RequestContext.HTTP.Method == http.MethodHead
	bad := func(code int) (events.APIGatewayV2HTTPResponse, error) {
		// Same JSON error envelope as the HTTP service's writeError, so
		// an API Gateway caller parses one shape on every path instead
		// of an empty body here. A HEAD answer carries the headers but
		// no body, as HEAD requires (and as the Python server sends it).
		msg := "bad request"
		switch code {
		case http.StatusRequestEntityTooLarge:
			msg = "request too large"
		case http.StatusBadGateway:
			msg = "response too large"
		}
		body, _ := json.Marshal(map[string]string{"error": msg})
		if isHead {
			return events.APIGatewayV2HTTPResponse{
				StatusCode:      code,
				Headers:         map[string]string{"Content-Type": "application/json", "Content-Length": fmt.Sprint(len(body))},
				Body:            "",
				IsBase64Encoded: false,
			}, nil
		}
		return events.APIGatewayV2HTTPResponse{
			StatusCode:      code,
			Headers:         map[string]string{"Content-Type": "application/json"},
			Body:            base64.StdEncoding.EncodeToString(body),
			IsBase64Encoded: true,
		}, nil
	}
	if h == nil || event.Version != "2.0" || event.RequestContext.HTTP.Method == "" || !strings.HasPrefix(event.RawPath, "/") || strings.HasPrefix(event.RawPath, "//") || strings.ContainsAny(event.RawPath, "?#\r\n") || strings.ContainsAny(event.RawQueryString, "\r\n#") {
		return bad(http.StatusBadRequest)
	}
	target := event.RawPath
	if event.RawQueryString != "" {
		target += "?" + event.RawQueryString
	}
	parsed, err := url.ParseRequestURI(target)
	if err != nil || parsed.IsAbs() || parsed.Host != "" {
		return bad(http.StatusBadRequest)
	}
	if len(event.Body) > base64.StdEncoding.EncodedLen(maxBody) {
		return bad(http.StatusRequestEntityTooLarge)
	}
	body := []byte(event.Body)
	if event.IsBase64Encoded {
		body, err = base64.StdEncoding.DecodeString(event.Body)
		if err != nil {
			return bad(http.StatusBadRequest)
		}
	}
	if len(body) > maxBody {
		return bad(http.StatusRequestEntityTooLarge)
	}
	req, err := http.NewRequestWithContext(ctx, event.RequestContext.HTTP.Method, "http://lambda.invalid"+target, bytes.NewReader(body))
	if err != nil {
		return bad(http.StatusBadRequest)
	}
	seen := make(map[string]bool, len(event.Headers))
	for name, value := range event.Headers {
		canonical := http.CanonicalHeaderKey(name)
		if seen[canonical] {
			return bad(http.StatusBadRequest)
		}
		seen[canonical] = true
		if !validHeader(name, value) || canonical == "Authorization" && strings.Contains(value, ",") || canonical == "Cookie" && len(event.Cookies) != 0 {
			return bad(http.StatusBadRequest)
		}
		req.Header.Set(name, value)
	}
	for _, cookie := range event.Cookies {
		if !validHeader("Cookie", cookie) {
			return bad(http.StatusBadRequest)
		}
		req.Header.Add("Cookie", cookie)
	}
	w := &response{header: make(http.Header)}
	h.ServeHTTP(w, req)
	if w.overflow {
		return bad(http.StatusBadGateway)
	}
	if w.status == 0 {
		w.status = http.StatusOK
	}
	out := events.APIGatewayV2HTTPResponse{StatusCode: w.status, Headers: map[string]string{}, Body: base64.StdEncoding.EncodeToString(w.body.Bytes()), IsBase64Encoded: true}
	for key, values := range w.header {
		if strings.EqualFold(key, "Set-Cookie") {
			out.Cookies = append(out.Cookies, values...)
			continue
		}
		out.Headers[key] = strings.Join(values, ", ")
	}
	if isHead {
		// A HEAD answer carries no body: the Python server sends the
		// headers with a Content-Length for the body it did not write,
		// and net/http does the same on a direct server. The Lambda
		// adapter must not leak the handler's JSON body here.
		if w.header.Get("Content-Length") == "" {
			out.Headers["Content-Length"] = fmt.Sprint(w.body.Len())
		}
		out.Body = ""
	}
	return out, nil
}
func validHeader(name, value string) bool {
	if name == "" {
		return false
	}
	for _, c := range value {
		if c == 127 || c < 32 && c != 9 {
			return false
		}
	}
	for _, c := range name {
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || strings.ContainsRune("!#$%&'*+-.^_`|~", c)) {
			return false
		}
	}
	return true
}
