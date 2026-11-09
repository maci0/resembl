//go:build js || wasip1

package main

import "os"

// shutdownSIGTERM falls back to os.Interrupt on js/wasm and wasip1,
// where syscall.SIGTERM is not defined. The package otherwise
// builds for these GOOS targets only via the signal-free path
// below; this file keeps the cross-compile green should a future
// GOOS=js build be attempted, without changing behavior on any
// claimed platform (linux, darwin, windows).
// It is a var because os.Interrupt is a variable, not a constant.
var shutdownSIGTERM os.Signal = os.Interrupt
