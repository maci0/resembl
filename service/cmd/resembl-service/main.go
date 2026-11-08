// Command resembl-service serves one pinned Resembl index over HTTP.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	service "github.com/maci0/resembl/service"
)

func main() {
	server, engine, err := newServer(os.Args[1:])
	if err != nil {
		log.Fatal(err)
	}
	// SIGTERM (systemd, Docker stop, k8s) must drain in-flight queries
	// instead of killing the process mid-request: ListenAndServe returns
	// http.ErrServerClosed on Shutdown, and in-flight custody slots and
	// engine calls complete rather than being orphaned.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		if err := server.Shutdown(shutdownCtx); err != nil {
			log.Printf("shutdown error: %v", err)
		}
		// Retire the engine's pooled connections once in-flight queries
		// have drained: the transport would otherwise hold its idle
		// sockets until the process exits, and a caller that builds
		// and discards servers in-process accumulates them.
		engine.CloseIdleConnections()
	}()
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		engine.CloseIdleConnections()
		log.Fatal(err)
	}
	engine.CloseIdleConnections()
}

// newServer parses the flags and builds the server without starting it.
// It also returns the pinned engine so the caller can retire its pooled
// connections on shutdown; on any construction failure after the engine
// is built, the engine is retired before the error is returned.
func newServer(args []string) (*http.Server, *service.PinnedSQLiteEngine, error) {
	flags := flag.NewFlagSet("resembl-service", flag.ContinueOnError)
	address := flags.String("listen", "127.0.0.1:8080", "listen address")
	engineURL := flags.String("engine", "http://127.0.0.1:8081", "isolated loopback Resembl engine")
	index := flags.String("index-sha256", "", "verified SQLite index snapshot digest")
	artifact := flags.String("artifact-sha256", "", "trusted runtime manifest digest")
	generation := flags.String("generation", "", "verified warm engine generation")
	state := flags.String("custody", "", "private persistent custody directory")
	tokenFile := flags.String("token-file", "", "private file containing an operator-generated 256-bit token")
	resource := flags.String("resource", "", "server-owned index resource name (required)")
	concurrency := flags.Int("concurrency", 4, "concurrent queries and custody slots, 1 to 64")
	timeout := flags.Duration("timeout", 30*time.Second, "per-request timeout")
	if err := flags.Parse(args); err != nil {
		return nil, nil, err
	}
	if *resource == "" {
		return nil, nil, errors.New("-resource is required")
	}
	// Fail closed: without a token the service denies every query, which is
	// safe but useless; on a routable interface it would also expose
	// unauthenticated /healthz and /metrics. Require a token file for any
	// non-loopback listen address so the operator must make the exposure
	// deliberate.
	if *tokenFile == "" && !isLoopbackListen(*address) {
		return nil, nil, fmt.Errorf("-token-file is required when -listen %s is not loopback", *address)
	}
	engine, err := service.NewPinnedSQLiteEngineWithTimeout(*engineURL, *index, *artifact, *generation, *timeout)
	if err != nil {
		return nil, nil, err
	}
	var authority service.Authorizer = service.DenyAll{}
	if *tokenFile != "" {
		authority, err = service.NewTokenAuthority(*tokenFile, *resource)
		if err != nil {
			engine.CloseIdleConnections()
			return nil, nil, err
		}
	}
	handler, err := service.New(authority, engine, *resource, *concurrency, *timeout)
	if err != nil {
		engine.CloseIdleConnections()
		return nil, nil, err
	}
	custody, err := service.NewFileCustody(*state, *concurrency)
	if err != nil {
		engine.CloseIdleConnections()
		return nil, nil, err
	}
	if err = handler.SetCustody(custody); err != nil {
		engine.CloseIdleConnections()
		return nil, nil, err
	}
	if *tokenFile == "" && isLoopbackListen(*address) {
		log.Print("warning: no -token-file configured; all queries will be denied")
	}
	return &http.Server{
		Addr:              *address,
		Handler:           handler,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       *timeout,
		WriteTimeout:      *timeout + 5*time.Second,
		IdleTimeout:       30 * time.Second,
		MaxHeaderBytes:    16 << 10,
	}, engine, nil
}

// isLoopbackListen reports whether address binds only loopback. An empty
// host or a wildcard binds every interface and is not loopback.
func isLoopbackListen(address string) bool {
	host, _, err := net.SplitHostPort(address)
	if err != nil {
		return false
	}
	host = strings.TrimSpace(host)
	if host == "" || host == "0.0.0.0" || host == "::" || host == "[::]" {
		return false
	}
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(strings.Trim(host, "[]"))
	if ip == nil {
		return false
	}
	return ip.IsLoopback()
}
