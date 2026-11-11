#!/usr/bin/python3
"""The session monitor's watcher: wakes the monitor when there is something to read.

The monitor session runs this in the background. Each line printed wakes it:

  CHANGED   hook files or the job events have grown past what the monitor last read
  TICK      the heartbeat, so the monitor checks in even when nothing changes

A line never names what changed: the monitor's status command reports that,
and a named session invites the monitor to check that session alone.

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

    offsets_path = os.path.join(state, "offsets.json")
    announced = None
    next_tick = time.monotonic() + tick
    while True:
        pending = monitor_lib.pending_changes(state)
        try:
            read_at = os.path.getmtime(offsets_path)
        except FileNotFoundError:
            read_at = None
        # Say each set of changes once per read by the monitor. While it is
        # mid-turn it cannot read them, and repeating the line would only queue
        # more wakes; once it has read, the same file growing again is news.
        if pending and (pending, read_at) != announced:
            print("CHANGED", flush=True)
        announced = (pending, read_at)
        if time.monotonic() >= next_tick:
            print("TICK", flush=True)
            next_tick = time.monotonic() + tick
        time.sleep(poll)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        sys.exit(130)
