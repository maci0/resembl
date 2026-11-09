//go:build unix

package main

import (
	"os"
	"syscall"
)

// shutdownSIGTERM is the graceful termination signal on Unix-like
// platforms, where SIGTERM is the contract systemd, Docker stop, and
// Kubernetes rely on. It lives behind a build constraint because
// syscall.SIGTERM is not defined on Windows or js/wasm; the Windows
// sibling falls back to os.Interrupt there. SIGPIPE is also absent
// on some of those targets, which is why this file does not name it.
// It is a var, not a const, so the Unix and non-Unix files can
// declare it with a common type (os.Signal) even though os.Interrupt
// is itself a variable, not a constant.
var shutdownSIGTERM os.Signal = syscall.SIGTERM
