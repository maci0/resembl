//go:build windows

package main

import "os"

// shutdownSIGTERM falls back to os.Interrupt on Windows, where
// syscall.SIGTERM does not exist as a deliverable signal and the
// only shutdown the OS sends the process is a console Ctrl+C /
// Ctrl+Break, both delivered as os.Interrupt by the runtime. This
// mirrors the prior behavior change that made the custody directory
// sync best-effort on Windows: the process must still build and
// shut down cleanly on the second claimed CI platform (windows-2025).
// It is a var because os.Interrupt is a variable, not a constant,
// so it cannot be a const.
var shutdownSIGTERM os.Signal = os.Interrupt
