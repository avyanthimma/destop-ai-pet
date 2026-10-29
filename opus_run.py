r"""
opus_run.py - run a command and tell Opus how it went.

    python opus_run.py npm run build
    python opus_run.py pytest -q
    python opus_run.py -- make all          (use -- if the command starts with a dash)

The command runs normally: its output still appears in this terminal, and the exit code is
passed through, so scripts and CI see the same result. When it finishes, the running Opus pet
reacts: a chime and a happy hop on success, or a sound and a one-line diagnosis on failure.

It talks to Opus by dropping a small file in %APPDATA%\Opus\inbox, so nothing goes over the network.
"""
from __future__ import annotations

import collections
import subprocess
import sys
import time

from companion import post_event

USAGE = "usage: python opus_run.py <command> [args...]"


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2

    command = subprocess.list2cmdline(argv)
    tail: collections.deque[str] = collections.deque(maxlen=40)  # the end of the output, for the diagnosis
    started = time.time()
    try:
        # shell=True so Windows finds npm.cmd, yarn.cmd and friends, exactly as typed in a terminal
        process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace", bufsize=1)
    except OSError as exc:
        print(f"opus_run: could not start {command}: {exc}", file=sys.stderr)
        code = 127
    else:
        try:
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                tail.append(line.rstrip("\n"))
            code = process.wait()
        except KeyboardInterrupt:
            process.terminate()
            code = 130

    try:
        post_event({
            "kind": "command",
            "command": command,
            "exit_code": code,
            "seconds": round(time.time() - started, 1),
            "tail": list(tail)[-30:],
        })
    except OSError as exc:
        print(f"opus_run: could not notify Opus: {exc}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
