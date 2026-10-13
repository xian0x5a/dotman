"""Stand-in for an interactive shell that runs one command as its foreground job.

Usage: job_control_shell.py STATUS_DIR COMMAND...

Start it as the session leader of a PTY. Like a job-control shell, it gives the
command its own process group, hands that group the terminal, and takes the
terminal back when the command exits. It then stays alive until killed, so the
kernel never SIGHUPs whatever the command left behind. The job's pid and
return code are published as files in STATUS_DIR.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path


def _publish(path: Path, text: str) -> None:
    staging = path.with_suffix(".staging")
    staging.write_text(text)
    staging.replace(path)


def _take_terminal() -> None:
    # A background group that changes the foreground group gets SIGTTOU unless
    # it blocks the signal, which is what job-control shells do.
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTTOU})
    os.tcsetpgrp(0, os.getpgrp())
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTTOU})


def main() -> None:
    status_dir = Path(sys.argv[1])
    # Hand over the terminal before exec, so the job never reads it from the background.
    job = subprocess.Popen(sys.argv[2:], process_group=0, preexec_fn=_take_terminal)
    _publish(status_dir / "job-pid", str(job.pid))
    returncode = job.wait()
    _take_terminal()
    _publish(status_dir / "job-returncode", str(returncode))
    while True:
        signal.pause()


if __name__ == "__main__":
    main()
