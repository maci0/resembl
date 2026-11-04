package main

import (
	"flag"
	"log"
	"net/http"
	"time"

	service "github.com/maci0/resembl/service"
)

func main() {
	address := flag.String("listen", "127.0.0.1:8080", "listen address")
	engineURL := flag.String("engine", "http://127.0.0.1:8081", "isolated loopback Resembl engine")
	index := flag.String("index-sha256", "", "verified SQLite index snapshot digest")
	artifact := flag.String("artifact-sha256", "", "trusted runtime manifest digest")
	generation := flag.String("generation", "", "verified warm engine generation")
	state := flag.String("custody", "", "private persistent custody directory")
	tokenFile := flag.String("token-file", "", "private file containing an operator-generated 256-bit token")
	resource := flag.String("resource", "unconfigured", "server-owned index resource")
	flag.Parse()
	engine, err := service.NewPinnedSQLiteEngine(*engineURL, *index, *artifact, *generation)
	if err != nil {
		log.Fatal(err)
	}
	var authority service.Authorizer = service.DenyAll{}
	if *tokenFile != "" {
		authority, err = service.NewTokenAuthority(*tokenFile, *resource)
		if err != nil {
			log.Fatal(err)
		}
	}
	handler, err := service.New(authority, engine, *resource, 4, 30*time.Second)
	if err != nil {
		log.Fatal(err)
	}
	custody, err := service.NewFileCustody(*state, 4)
	if err != nil {
		log.Fatal(err)
	}
	if err = handler.SetCustody(custody); err != nil {
		log.Fatal(err)
	}
	server := &http.Server{Addr: *address, Handler: handler, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 30 * time.Second, WriteTimeout: 35 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 16 << 10}
	log.Fatal(server.ListenAndServe())
}
