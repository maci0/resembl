"""Shared helpers for the test suite."""

import os
import signal
import subprocess


def serve_stop_flags() -> dict:
    """Return ``Popen`` kwargs that let the platform stop ``serve`` cleanly.

    POSIX sends SIGTERM, which the serve command turns into a shutdown (server
    close plus port-file removal).  Windows has no SIGTERM: ``Popen.terminate``
    calls TerminateProcess, which runs no cleanup at all, so what a service
    manager sends there is CTRL_BREAK_EVENT, which raises KeyboardInterrupt in
    the child and takes the same clean path.  A child needs its own process
    group to be addressed that way without the event reaching the test runner
    that started it.
    """
    if os.name != "nt":
        return {}
    # Windows-only names; typeshed gates both on the platform.
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]


def process_stop(process: subprocess.Popen) -> None:
    """Ask *process*, started with :func:`serve_stop_flags`, to shut down.

    The signal is the one the platform's service managers send, so the child
    runs its own shutdown path instead of being killed under it.
    """
    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
    else:
        process.terminate()
