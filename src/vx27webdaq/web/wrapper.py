"""Runs the DAQ program for the web server, apart from it, so that a run goes on when the server stops or
restarts:

    python -m vx27webdaq.web.wrapper --pid-file P --exit-file E -- python -m vx27webdaq.daq run RUN_FOLDER

The server starts this in a session of its own, with its output going to the run's daq.log. It
starts the command (which prints into the same log), writes the command's pid to P, passes SIGTERM, SIGINT
and SIGHUP on to it, waits, and writes the exit code to E (a negative code: ended by that signal). A server
that starts later finds the run through these two files. The signals are taken over before the command is
started: a Stop that comes while it starts is passed on to it as soon as it is there (it used to end the
wrapper itself, leaving the DAQ program running without anyone to pass Stop on).
"""

from __future__ import annotations

import signal

FORWARDED = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
_early: list[int] = []          # signals that came before the command was started
_target = None                  # the command's process, once started


def _forward(number, _frame) -> None:
    if _target is None:
        _early.append(number)
    else:
        _target.send_signal(number)


def _take_signals() -> None:
    for sig in FORWARDED:
        signal.signal(sig, _forward)


if __name__ == "__main__":      # as early as possible: before the imports below
    _take_signals()

import argparse                 # noqa: E402
import os                       # noqa: E402
import subprocess               # noqa: E402
import sys                      # noqa: E402


def write(path: str, text: str) -> None:
    tmp = f"{path}.part"
    with open(tmp, "w") as f:
        f.write(text + "\n")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    global _target
    _take_signals()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pid-file", required=True)
    ap.add_argument("--exit-file", required=True)
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        ap.error("no command after --")
    try:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL)
    except OSError as err:
        print(f"ERROR: cannot start {command[0]}: {err}", flush=True)
        write(args.exit_file, "127")
        return 127
    _target = proc
    write(args.pid_file, str(proc.pid))
    for number in _early:           # a Stop while it was being started: now it is there
        proc.send_signal(number)
    code = proc.wait()
    sys.stdout.flush()
    write(args.exit_file, str(code))
    return 0 if code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
