#!/usr/bin/python3
"""The session monitor's hook: records what each session is waiting on.

Claude Code runs this in every session at the events named in settings.json,
with the event as JSON on stdin and the monitor's state directory as the one
argument. It appends at most one line to state/hooks/<session id>.jsonl.

It must never disturb the session it runs in. It prints nothing, because some
events feed a hook's output back to the model or read it as a decision, and it
exits 0 on every path, including its own failures.
"""

import json
import os
import sys


def main():
    if len(sys.argv) < 2:
        return
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import monitor_lib

    session = os.environ.get("CLAUDE_CODE_HOST_SESSION_ID", "")
    if not monitor_lib.SESSION_ID.match(session):
        return
    event = json.load(sys.stdin)
    if not isinstance(event, dict):
        return

    directory = os.path.join(sys.argv[1], "hooks")
    path = os.path.join(directory, session + ".jsonl")
    # Only the permission events need the file's last records, so nothing else
    # pays for the read.
    reads_recent = event.get("hook_event_name") in ("PostToolUse", "Notification")
    record = monitor_lib.record_for(event, monitor_lib.recent_records(path) if reads_recent else [])
    if record is None:
        return

    record = dict({"ts": monitor_lib.now_iso()}, **record)
    if event.get("tool_use_id"):
        record["tool_use_id"] = event["tool_use_id"]
    line = monitor_lib.encode_record(record)

    os.makedirs(directory, mode=0o700, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, line)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
