//go:build unix || windows || js || wasip1

package main

import (
	"testing"
)

// TestShutdownSIGTERMNonNil pins the portability contract that
// shutdownSIGTERM is always a usable os.Signal for
// signal.NotifyContext on every GOOS this package cross-compiles
// for. On Unix it is syscall.SIGTERM; on Windows/js/wasip1 it is
// os.Interrupt. The signal must not be nil, which would silently
// disarm the graceful shutdown (no crash, just no drain).
func TestShutdownSIGTERMNonNil(t *testing.T) {
	if shutdownSIGTERM == nil {
		t.Fatal("shutdownSIGTERM is nil; signal.NotifyContext would not fire")
	}
}
