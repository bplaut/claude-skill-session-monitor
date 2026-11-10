#!/usr/bin/python3
"""Deterministic helpers for the session monitor.

Used by hook.py (which Claude Code runs in every session), by poller.sh, by
watch.py, and by the monitor session through the command line at the bottom.
Standard library only, and Python 3.9 syntax, because the hook runs under the
system interpreter.
"""

import datetime
import json
import os
import re
import sys

# The id the desktop app gives a session; it is also the id in session links.
# Hook file names are built from it, so anything else is refused.
SESSION_ID = re.compile(r"^local_[A-Za-z0-9-]+$")

BOX_TOOL = "mcp__ccd_turn__wrap_up"

# A background task finishing reaches the session as a prompt too. These are
# the openings of such prompts; a prompt that starts with one is not the user.
AUTOMATED_PROMPT_PREFIXES = ("<task-notification", "[SYSTEM NOTIFICATION", "<system-reminder")

FINISHED = "Completed"
FAILED = ("Failed", "Aborted", "Terminated")

# One record is written with a single write call; this keeps it small enough
# that appends from several processes cannot interleave.
RECORD_LIMIT = 4096

ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def first_word(command):
    """The program a shell command runs, without its arguments.

    Leading VAR=value assignments are skipped, since that is where a command
    line carries credentials.
    """
    for token in str(command).split():
        if not ENV_ASSIGNMENT.match(token):
            return os.path.basename(token)[:40]
    return ""


def request_text(tool_name, tool_input):
    """What a permission request is about, in a form safe to show on a page."""
    if not isinstance(tool_input, dict):
        return ""
    if tool_name == "AskUserQuestion":
        questions = tool_input.get("questions") or []
        if questions and isinstance(questions[0], dict):
            return str(questions[0].get("question", ""))[:200]
        return ""
    if "command" in tool_input:
        return first_word(tool_input["command"])
    if "file_path" in tool_input:
        return os.path.basename(str(tool_input["file_path"]))[:80]
    return ""


def is_automated_prompt(prompt):
    return str(prompt or "").lstrip().startswith(AUTOMATED_PROMPT_PREFIXES)


def record_for(event, last_kind):
    """The record a hook event should append to its session's file, or None.

    `last_kind` is the kind of the last record already in the file. A subagent
    reports only permission traffic: its prompts stop the parent session, but
    its turn ends and tool calls are not the session's.
    """
    name = event.get("hook_event_name")
    if name == "PermissionRequest":
        tool_name = event.get("tool_name")
        return {"kind": "request", "tool_name": tool_name, "text": request_text(tool_name, event.get("tool_input"))}
    if name == "Notification":
        if event.get("notification_type") == "permission_prompt":
            return {"kind": "waiting"}
        return None
    if name == "PostToolUse":
        return {"kind": "resolved"} if last_kind == "waiting" else None
    if event.get("agent_id"):
        return None
    if name == "PreToolUse":
        if event.get("tool_name") != BOX_TOOL:
            return None
        tool_input = event.get("tool_input") or {}
        actions = [str(a.get("label", ""))[:60] for a in tool_input.get("actions") or [] if isinstance(a, dict)]
        ask = tool_input.get("ask")
        return {
            "kind": "box",
            "ask": str(ask)[:300] if ask else None,
            "actions": actions,
            "blocked": bool(tool_input.get("blocked")),
            "recap": str(tool_input.get("recap", "")),
        }
    if name == "Stop":
        return {"kind": "stop"}
    if name == "UserPromptSubmit":
        return None if is_automated_prompt(event.get("prompt")) else {"kind": "reply"}
    return None


def encode_record(record):
    """One JSON line of at most RECORD_LIMIT bytes; a long recap is cut to fit."""
    line = (json.dumps(record) + "\n").encode()
    if len(line) > RECORD_LIMIT and record.get("recap"):
        excess = len(line) - RECORD_LIMIT
        recap = record["recap"]
        # Every character costs at least one byte, so this cut is always enough
        # for ASCII; the loop covers text whose characters take more.
        keep = max(0, len(recap) - excess - 3)
        while True:
            line = (json.dumps(dict(record, recap=recap[:keep] + "...")) + "\n").encode()
            if len(line) <= RECORD_LIMIT or keep == 0:
                break
            keep = max(0, keep - (len(line) - RECORD_LIMIT))
    return line


def read_records(data):
    """The records in the complete lines of a hook file's bytes, and their length."""
    end = data.rfind(b"\n") + 1
    records = []
    for line in data[:end].splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records, end


def last_kind(path):
    """The kind of the last complete record in a hook file, or None."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 2 * RECORD_LIMIT))
            records, _ = read_records(f.read())
    except OSError:
        return None
    return records[-1].get("kind") if records else None


def open_items(records):
    """What a session is waiting on the user for, from its hook records.

    The question is the latest box record, while it asks something and no
    reply follows it. A prompt is open while `waiting` is the last record; its
    text comes from the request just before it.
    """
    question = None
    boxes = [i for i, r in enumerate(records) if r.get("kind") == "box"]
    if boxes:
        box = records[boxes[-1]]
        replied = any(r.get("kind") == "reply" for r in records[boxes[-1] + 1 :])
        if (box.get("ask") or box.get("blocked")) and not replied:
            question = {key: box.get(key) for key in ("ask", "actions", "recap", "blocked", "ts")}

    prompt = None
    if records and records[-1].get("kind") == "waiting":
        requests = [r for r in records if r.get("kind") == "request"]
        request = requests[-1] if requests else {}
        prompt = {"tool_name": request.get("tool_name"), "text": request.get("text", ""), "ts": records[-1].get("ts")}

    return {"question": question, "prompt": prompt}


def diff_jobs(before, after):
    """The transitions between two listings, each a dict of job name to phase.

    A job that finished and was then cleaned up is not reported again, and a
    job absent from both listings is nothing.
    """
    events = []
    for name, phase in after.items():
        was = before.get(name)
        if phase == FINISHED and was != FINISHED:
            events.append(("DONE", name))
        elif phase in FAILED and was not in FAILED:
            events.append(("FAILED", name))
        elif was is None:
            events.append(("NEW", name))
    for name, was in before.items():
        if name not in after and was != FINISHED and was not in FAILED:
            events.append(("GONE", name))
    return events


def write_json(path, value):
    """Replace a JSON file in one step, so a reader never sees half of it."""
    scratch = path + ".tmp"
    with open(scratch, "w") as f:
        json.dump(value, f, indent=1)
    os.replace(scratch, path)


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def record_jobs(state, context, kubectl_json, now=None):
    """Store one cluster's job listing and append its transitions to events.log.

    `kubectl_json` is the output of `kubectl get vcjob -o json`. Anything else
    raises ValueError and leaves the stored listing and its `okAt` untouched,
    which is how a failed read stays visible. The first listing of a cluster
    reports nothing, since every job in it would look new.
    """
    now = now or now_iso()
    listing = json.loads(kubectl_json)
    if not isinstance(listing, dict) or not isinstance(listing.get("items"), list):
        raise ValueError("not a kubectl job listing")
    after = {}
    for item in listing["items"]:
        metadata = item.get("metadata") or {}
        phase = ((item.get("status") or {}).get("state") or {}).get("phase") or "Unknown"
        after[metadata["name"]] = {"phase": phase, "created": metadata.get("creationTimestamp")}

    jobs_path = os.path.join(state, "jobs.json")
    stored = read_json(jobs_path, {"clusters": {}})
    previous = stored["clusters"].get(context)
    if previous is not None:
        before = {name: job["phase"] for name, job in previous["jobs"].items()}
        events = diff_jobs(before, {name: job["phase"] for name, job in after.items()})
        if events:
            with open(os.path.join(state, "events.log"), "a") as log:
                for event, name in events:
                    log.write("%s %s %s %s\n" % (now, event, context, name))
    stored["clusters"][context] = {"okAt": now, "jobs": after}
    write_json(jobs_path, stored)


def hook_files(state):
    """Session id and path of each hook file, skipping names that are not session ids."""
    directory = os.path.join(state, "hooks")
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return []
    found = []
    for name in names:
        session, extension = os.path.splitext(name)
        if extension == ".jsonl" and SESSION_ID.match(session):
            found.append((session, os.path.join(directory, name)))
    return found


def read_offsets(state):
    offsets = read_json(os.path.join(state, "offsets.json"), {})
    return {"hooks": offsets.get("hooks", {}), "events": offsets.get("events", 0)}


def status(state, only=None):
    """Everything the monitor needs from the state directory, as one dict.

    Marks what it read as seen: `changed` is true for a session whose file
    gained complete records since the last call, and each job event is
    reported once. The offsets stored are the ends of what was actually read,
    so a record that lands during the call is reported by the next one.

    `only` is the list of session ids the monitor is showing. Given, it limits
    the report to them and is stored as the watch list, so the watcher stops
    waking the monitor for other sessions, the monitor's own among them: its
    turn ends would otherwise wake it again without end.
    """
    offsets = read_offsets(state)
    os.makedirs(state, exist_ok=True)
    if only is not None:
        write_json(os.path.join(state, "watching.json"), sorted(only))
    sessions = {}
    for session, path in hook_files(state):
        with open(path, "rb") as f:
            records, end = read_records(f.read())
        if only is None or session in only:
            last = records[-1] if records else {}
            sessions[session] = dict(
                open_items(records),
                changed=end > offsets["hooks"].get(session, 0),
                last_kind=last.get("kind"),
                last_ts=last.get("ts"),
            )
        offsets["hooks"][session] = end

    job_events = []
    try:
        with open(os.path.join(state, "events.log"), "rb") as f:
            f.seek(offsets["events"])
            data = f.read()
    except FileNotFoundError:
        data = b""
    end = data.rfind(b"\n") + 1
    for line in data[:end].decode().splitlines():
        parts = line.split()
        if len(parts) == 4:
            job_events.append(dict(zip(("ts", "event", "cluster", "job"), parts)))
    offsets["events"] += end

    write_json(os.path.join(state, "offsets.json"), offsets)
    jobs = read_json(os.path.join(state, "jobs.json"), {"clusters": {}})
    return {"now": now_iso(), "sessions": sessions, "job_events": job_events, "clusters": jobs["clusters"]}


def pending_changes(state):
    """Names of what has grown past the monitor's stored offsets: session ids, and "jobs".

    Read-only, so the watcher can call it as often as it likes. Once the
    monitor has stored a watch list, sessions outside it are not reported.
    """
    offsets = read_offsets(state)
    watching = read_json(os.path.join(state, "watching.json"), None)
    pending = [
        s
        for s, path in hook_files(state)
        if (watching is None or s in watching) and os.path.getsize(path) > offsets["hooks"].get(s, 0)
    ]
    try:
        if os.path.getsize(os.path.join(state, "events.log")) > offsets["events"]:
            pending.append("jobs")
    except FileNotFoundError:
        pass
    return pending


USAGE = """usage:
  monitor_lib.py status <state-dir> [--only id ...]  print the state as JSON and mark it seen;
                                                     --only limits it to, and watches, those sessions
  monitor_lib.py pending <state-dir>                print what changed since the last status
  monitor_lib.py record-jobs <state-dir> <context>  store a `kubectl get vcjob -o json` listing from stdin
"""


def main(argv):
    if len(argv) >= 3 and argv[1] == "status":
        only = argv[4:] if len(argv) >= 4 and argv[3] == "--only" else None
        json.dump(status(argv[2], only), sys.stdout, indent=1)
        print()
    elif len(argv) >= 3 and argv[1] == "pending":
        print(" ".join(pending_changes(argv[2])))
    elif len(argv) >= 4 and argv[1] == "record-jobs":
        record_jobs(argv[2], argv[3], sys.stdin.read())
    else:
        sys.stderr.write(USAGE)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
