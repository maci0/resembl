// Command resembl-service serves one pinned Resembl index over HTTP.
package main

import (
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"time"

	service "github.com/maci0/resembl/service"
)

func main() {
	server, err := newServer(os.Args[1:])
	if err != nil {
		log.Fatal(err)
	}
	log.Fatal(server.ListenAndServe())
}

// newServer parses the flags and builds the server without starting it.
func newServer(args []string) (*http.Server, error) {
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
		return nil, err
	}
	if *resource == "" {
		return nil, errors.New("-resource is required")
	}
	engine, err := service.NewPinnedSQLiteEngine(*engineURL, *index, *artifact, *generation)
	if err != nil {
		return nil, err
	}
	var authority service.Authorizer = service.DenyAll{}
	if *tokenFile != "" {
		authority, err = service.NewTokenAuthority(*tokenFile, *resource)
		if err != nil {
			return nil, err
		}
	}
	handler, err := service.New(authority, engine, *resource, *concurrency, *timeout)
	if err != nil {
		return nil, err
	}
	custody, err := service.NewFileCustody(*state, *concurrency)
	if err != nil {
		return nil, err
	}
	if err = handler.SetCustody(custody); err != nil {
		return nil, err
	}
	return &http.Server{
		Addr:              *address,
		Handler:           handler,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       *timeout,
		WriteTimeout:      *timeout + 5*time.Second,
		IdleTimeout:       30 * time.Second,
		MaxHeaderBytes:    16 << 10,
	}, nil
}
