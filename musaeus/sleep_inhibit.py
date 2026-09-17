#!/usr/bin/env python3
"""Keep the machine awake for the length of a long run.

The problem this solves
-----------------------
Grey found it on 2026-09-15, and it is a genuine trap rather than a
preference. Two settings pull against each other and there was no position
that satisfied both:

    Caffeine ON   the machine stays awake -- but Caffeine works by resetting
                  the X11 screensaver idle counter, which is the same counter
                  IdleThrottle reads. The throttle then never sees its 40s of
                  idle, SIGSTOPs the encoder and never resumes it. Measured
                  2026-09-14: a five-track CAR build made zero progress in
                  five hours.

    Caffeine OFF  the throttle works correctly -- but the machine sleeps and
                  the job stops anyway.

The 2026-09-14 LUFS bake only completed because Caffeine was off AND the
machine happened not to sleep. That is luck, not configuration, and it is not
something to build a 44-hour CAR build on.

The fix
-------
`systemd-inhibit --what=sleep:idle --mode=block` blocks sleep WITHOUT
touching the idle counter. The machine stays up; the throttle still sees real
idle time and still yields the moment Grey touches the keyboard. Both
behaviours, instead of choosing.

Deliberately re-exec rather than a background helper: the inhibitor lock is
held by a process, so tying it to *this* process means it cannot outlive the
run. A `systemd-inhibit sleep infinity` child would survive a SIGKILL of the
parent and leave the machine unable to sleep for good -- the failure mode
here is silent and lasts until somebody notices their laptop never suspends.

Never fatal. A machine without systemd, or a systemd that refuses the
inhibitor, must still be able to run a bake.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys

logger = logging.getLogger(__name__)

#: Set in the re-exec'd child so it does not wrap itself again, for ever.
_GUARD = "MUSAEUS_SLEEP_INHIBITED"

#: Honours the same off-switch convention as MUSAEUS_NO_IDLE_THROTTLE.
_DISABLE = "MUSAEUS_NO_SLEEP_INHIBIT"


def already_inhibited() -> bool:
    return bool(os.environ.get(_GUARD))


def reexec_under_inhibitor(why: str) -> None:
    """Re-run this process under systemd-inhibit, once, and never return.

    Call it first thing in a long-running tool's main(). On the second pass
    -- inside the inhibited child -- it returns immediately and the tool
    proceeds normally.

    Returns (rather than exiting) without doing anything when:
      - the guard env var is set, so we are already the inhibited child
      - MUSAEUS_NO_SLEEP_INHIBIT is set
      - systemd-inhibit is not on PATH
      - the exec itself fails

    In every one of those cases the tool still runs. An inhibitor is a
    convenience for unattended work, not a precondition for doing the work.
    """
    if already_inhibited() or os.environ.get(_DISABLE):
        return
    exe = shutil.which("systemd-inhibit")
    if not exe:
        logger.info("[sleep] systemd-inhibit not available -- the machine may sleep mid-run")
        return

    argv = [
        exe,
        "--what=sleep:idle",
        "--who=MUSAEUS",
        f"--why={why}",
        "--mode=block",
        sys.executable,
        *sys.argv,
    ]
    env = dict(os.environ, **{_GUARD: "1"})
    print(f"[sleep] holding a sleep inhibitor for: {why}", flush=True)
    try:
        os.execve(exe, argv, env)
    except OSError as exc:
        # execve only returns on failure. Carry on uninhibited rather than
        # refusing to do the work.
        logger.warning("[sleep] could not re-exec under systemd-inhibit: %s", exc)
