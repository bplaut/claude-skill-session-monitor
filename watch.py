#!/usr/bin/python3
"""The session monitor's watcher: wakes the monitor when there is something to read.

The monitor session runs this in the background. Each line printed wakes it:

  CHANGED <names>   hook files (by session id) or the job events ("jobs") have
                    grown past what the monitor last read
  TICK              the heartbeat, so the monitor checks in even when nothing changes

It only reads inside the state directory and uses no network.

usage: watch.py <state-dir> [poll-seconds] [tick-seconds]
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import monitor_lib  # noqa: E402


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(__doc__)
        return 2
    state = argv[1]
    poll = float(argv[2]) if len(argv) > 2 else 15.0
    tick = float(argv[3]) if len(argv) > 3 else 1200.0

    announced = []
    next_tick = time.monotonic() + tick
    while True:
        pending = monitor_lib.pending_changes(state)
        # Say each set of changes once. The monitor may be mid-turn and unable
        # to read them yet; repeating the line would only queue more wakes.
        if pending and pending != announced:
            print("CHANGED " + " ".join(pending), flush=True)
        announced = pending
        if time.monotonic() >= next_tick:
            print("TICK", flush=True)
            next_tick = time.monotonic() + tick
        time.sleep(poll)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        sys.exit(130)
