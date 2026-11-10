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
# "Gone" is this module's own phase for a job that vanished while running.
ENDED = (FINISHED, "Gone") + FAILED

# One record is written with a single write call; this keeps it small enough
# that appends from several processes cannot interleave.
RECORD_LIMIT = 4096

# How long a job stays in the stored listing after the cluster drops it, so a
# session waiting on it can still be told how it ended.
KEEP_ENDED = datetime.timedelta(hours=24)

# The board treats a check as overdue once this long has passed without one.
NEXT_CHECK = datetime.timedelta(minutes=25)

ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def now_iso():
    """Local time with its UTC offset, which the board page needs to compare times."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def first_word(command):
    """The program a shell command runs, without its arguments.

    Leading VAR=value assignments are skipped, since that is where a command
    line carries credentials.
    """
    if not isinstance(command, str):
        return ""
    for token in command.split():
        if not ENV_ASSIGNMENT.match(token):
            return os.path.basename(token)[:40]
    return ""


def request_text(tool_name, tool_input):
    """What a permission request is about, in a form safe to show on a page."""
    if not isinstance(tool_input, dict):
        return ""
    if tool_name == "AskUserQuestion":
        questions = tool_input.get("questions")
        if isinstance(questions, list) and questions and isinstance(questions[0], dict):
            return str(questions[0].get("question", ""))[:200]
        return ""
    if "command" in tool_input:
        return first_word(tool_input["command"])
    if "file_path" in tool_input:
        return os.path.basename(str(tool_input["file_path"]))[:80]
    return ""


def is_automated_prompt(prompt):
    """True for a prompt the user did not type; an empty one counts as automated."""
    text = str(prompt or "").lstrip()
    return not text or text.startswith(AUTOMATED_PROMPT_PREFIXES)


def open_prompt(records):
    """The latest `waiting` record that nothing has closed yet, or None.

    A prompt is closed by the tool call it was asking about (a `resolved`
    record for the same agent and tool), by the session's turn ending, or by
    the user replying. Other traffic, such as another agent's requests, leaves
    it open.
    """
    for index in range(len(records) - 1, -1, -1):
        record = records[index]
        if record.get("kind") != "waiting":
            continue
        for later in records[index + 1 :]:
            kind = later.get("kind")
            if kind in ("stop", "reply"):
                return None
            if kind == "resolved" and (later.get("agent_id"), later.get("tool_name")) == (
                record.get("agent_id"),
                record.get("tool_name"),
            ):
                return None
        return record
    return None


def record_for(event, recent):
    """The record a hook event should append to its session's file, or None.

    `recent` is the last few records already in the file; only the permission
    events read it. A subagent reports only permission traffic: its prompts
    stop the parent session, but its turn ends and tool calls are not the
    session's.
    """
    name = event.get("hook_event_name")
    agent = event.get("agent_id") or None
    if name == "PermissionRequest":
        tool_name = event.get("tool_name")
        return {
            "kind": "request",
            "tool_name": tool_name,
            "text": request_text(tool_name, event.get("tool_input")),
            "agent_id": agent,
        }
    if name == "Notification":
        if event.get("notification_type") != "permission_prompt":
            return None
        # The notification does not say which request it is about; it follows
        # the request that is still unanswered, which is the latest one.
        requests = [r for r in recent if r.get("kind") == "request"]
        request = requests[-1] if requests else {}
        return {
            "kind": "waiting",
            "tool_name": request.get("tool_name"),
            "text": request.get("text", ""),
            "agent_id": request.get("agent_id"),
        }
    if name == "PostToolUse":
        prompt = open_prompt(recent)
        if prompt and (prompt.get("agent_id"), prompt.get("tool_name")) == (agent, event.get("tool_name")):
            return {"kind": "resolved", "tool_name": event.get("tool_name"), "agent_id": agent}
        return None
    if agent:
        return None
    if name == "PreToolUse":
        if event.get("tool_name") != BOX_TOOL:
            return None
        tool_input = event.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        actions = tool_input.get("actions")
        labels = [str(a.get("label", ""))[:60] for a in actions if isinstance(a, dict)] if isinstance(actions, list) else []
        ask = tool_input.get("ask")
        return {
            "kind": "box",
            "ask": str(ask)[:300] if ask else None,
            "actions": labels,
            "blocked": bool(tool_input.get("blocked")),
            "recap": str(tool_input.get("recap") or ""),
        }
    if name == "Stop":
        return {"kind": "stop"}
    if name == "UserPromptSubmit":
        # Either kind starts a turn; only a reply is the user answering. A reply
        # keeps the start of its text, so an automated prompt mistaken for one
        # can be recognized in the file and its opening added to the list above.
        prompt = event.get("prompt")
        if is_automated_prompt(prompt):
            return {"kind": "auto"}
        return {"kind": "reply", "head": " ".join(str(prompt).split())[:40]}
    return None


def encode_record(record):
    """One JSON line of at most RECORD_LIMIT bytes.

    Text is cut to fit, least important first: the recap, then the button
    labels, then the question. JSON escapes some characters to many bytes, so
    each cut is checked against the encoded length rather than computed.
    """
    record = dict(record)

    def encoded():
        return (json.dumps(record) + "\n").encode()

    line = encoded()
    while len(line) > RECORD_LIMIT and record.get("recap"):
        record["recap"] = record["recap"][: len(record["recap"]) // 2]
        line = encoded()
    if len(line) > RECORD_LIMIT and record.get("actions"):
        record["actions"] = []
        line = encoded()
    while len(line) > RECORD_LIMIT and record.get("ask"):
        record["ask"] = record["ask"][: len(record["ask"]) // 2]
        line = encoded()
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


def recent_records(path):
    """The last few complete records of a hook file; an empty list if it is missing."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 8 * RECORD_LIMIT))
            records, _ = read_records(f.read())
    except OSError:
        return []
    return records


def turn_state(records):
    """Whether a turn is in flight, from the session's own turn markers, or None.

    A prompt (the user's or an automated one) starts a turn and a stop ends
    it. The hook writes these as they happen, so this is ahead of the app's
    session list, which can still report a turn as running just after its
    stop.
    """
    for record in reversed(records):
        kind = record.get("kind")
        if kind == "stop":
            return {"state": "ended", "since": record.get("ts")}
        if kind in ("reply", "auto"):
            return {"state": "in_flight", "since": record.get("ts")}
    return None


def open_items(records):
    """What a session is waiting on the user for, from its hook records.

    The question is the latest box record, while it asks something and no
    reply follows it. The prompt is the latest permission prompt still open.
    The turn is whether the session is mid-turn.
    """
    question = None
    boxes = [i for i, r in enumerate(records) if r.get("kind") == "box"]
    if boxes:
        box = records[boxes[-1]]
        replied = any(r.get("kind") == "reply" for r in records[boxes[-1] + 1 :])
        if (box.get("ask") or box.get("blocked")) and not replied:
            question = {key: box.get(key) for key in ("ask", "actions", "recap", "blocked", "ts")}

    prompt = open_prompt(records)
    if prompt is not None:
        prompt = {key: prompt.get(key) for key in ("tool_name", "text", "ts")}
    return {"question": question, "prompt": prompt, "turn": turn_state(records)}


def seconds_since(ts, now):
    """Whole seconds from an ISO time to `now`, or None if `ts` is not one."""
    try:
        then = datetime.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.astimezone()
    return int((now - then).total_seconds())


def diff_jobs(before, after):
    """The transitions between two listings, each a dict of job name to phase.

    A job that ended and was then cleaned up is not reported again, and a job
    absent from both listings is nothing. A name that ended and shows up alive
    again is a new job: launch stages reuse names.
    """
    events = []
    for name, phase in after.items():
        was = before.get(name)
        if phase == FINISHED and was != FINISHED:
            events.append(("DONE", name))
        elif phase in FAILED and was not in FAILED:
            events.append(("FAILED", name))
        elif was is None or (was in ENDED and phase not in ENDED):
            events.append(("NEW", name))
    for name, was in before.items():
        if name not in after and was not in ENDED:
            events.append(("GONE", name))
    return events


def write_json(path, value):
    """Replace a JSON file in one step, so a reader never sees half of it."""
    scratch = "%s.%d.tmp" % (path, os.getpid())
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

    A job the cluster no longer lists stays in the stored listing for a day
    with how it ended: its last phase, or "Gone" if it vanished while running.
    """
    now = now or now_iso()
    listing = json.loads(kubectl_json)
    if not isinstance(listing, dict) or not isinstance(listing.get("items"), list):
        raise ValueError("not a kubectl job listing")
    listed = {}
    for item in listing["items"]:
        metadata = item.get("metadata") if isinstance(item, dict) else None
        if not isinstance(metadata, dict) or not metadata.get("name"):
            raise ValueError("a job in the listing has no name")
        phase = ((item.get("status") or {}).get("state") or {}).get("phase") or "Unknown"
        listed[metadata["name"]] = {"phase": phase, "created": metadata.get("creationTimestamp")}

    jobs_path = os.path.join(state, "jobs.json")
    stored = read_json(jobs_path, {"clusters": {}})
    previous = stored["clusters"].get(context)
    before = previous["jobs"] if previous else {}

    jobs = {}
    for name, job in listed.items():
        if job["phase"] in ENDED:
            was = before.get(name, {})
            job["endedAt"] = was["endedAt"] if was.get("phase") == job["phase"] and "endedAt" in was else now
        jobs[name] = job
    for name, was in before.items():
        if name in listed:
            continue
        if "endedAt" not in was:
            jobs[name] = dict(was, phase=was["phase"] if was["phase"] in ENDED else "Gone", endedAt=now)
        elif datetime.datetime.fromisoformat(now) - datetime.datetime.fromisoformat(was["endedAt"]) < KEEP_ENDED:
            jobs[name] = was

    if previous is not None:
        events = diff_jobs(
            {name: job["phase"] for name, job in before.items()},
            {name: job["phase"] for name, job in listed.items()},
        )
        if events:
            with open(os.path.join(state, "events.log"), "a") as log:
                for event, name in events:
                    log.write("%s %s %s %s\n" % (now, event, context, name))
    stored["clusters"][context] = {"okAt": now, "jobs": jobs}
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


def times():
    """The board's stamp values for a check made now: a pure function of the clock."""
    now = datetime.datetime.now().astimezone()
    return {
        "now": now.isoformat(timespec="seconds"),
        "checked_at": now.strftime("%H:%M"),
        "next_by": (now + NEXT_CHECK).isoformat(timespec="seconds"),
    }


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

    `checked_at`, `next_by` and `cluster_ok_at` are the values to write on the
    board as they stand, so the monitor never has to compute a time.
    """
    offsets = read_offsets(state)
    os.makedirs(state, exist_ok=True)
    if only is not None:
        write_json(os.path.join(state, "watching.json"), sorted(only))
    now = datetime.datetime.now().astimezone()
    sessions = {}
    for session, path in hook_files(state):
        with open(path, "rb") as f:
            records, end = read_records(f.read())
        if only is None or session in only:
            last = records[-1] if records else {}
            items = open_items(records)
            if items["turn"]:
                items["turn"]["age_seconds"] = seconds_since(items["turn"]["since"], now)
            sessions[session] = dict(
                items,
                changed=end > offsets["hooks"].get(session, 0),
                last_kind=last.get("kind"),
                last_ts=last.get("ts"),
            )
        offsets["hooks"][session] = end

    job_events = []
    try:
        with open(os.path.join(state, "events.log"), "rb") as f:
            # A log shorter than what was read before was replaced; start over.
            if os.fstat(f.fileno()).st_size < offsets["events"]:
                offsets["events"] = 0
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

    stored = read_json(os.path.join(state, "jobs.json"), {"clusters": {}})["clusters"]
    clusters = {}
    for context, cluster in stored.items():
        jobs = {name: {k: v for k, v in job.items() if k != "created"} for name, job in cluster["jobs"].items()}
        clusters[context] = {"okAt": cluster["okAt"], "jobs": jobs}
    return dict(
        times(),
        cluster_ok_at=max((c["okAt"] for c in clusters.values()), default=None),
        sessions=sessions,
        job_events=job_events,
        clusters=clusters,
    )


def pending_changes(state):
    """Names of what has changed past the monitor's stored offsets: session ids, and "jobs".

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
        if os.path.getsize(os.path.join(state, "events.log")) != offsets["events"]:
            pending.append("jobs")
    except FileNotFoundError:
        pass
    return pending


USAGE = """usage:
  monitor_lib.py status <state-dir> [--only id ...]  print the state as JSON and mark it seen;
                                                     --only limits it to, and watches, those sessions
  monitor_lib.py times                               print the board's stamp values for now, touching nothing
  monitor_lib.py pending <state-dir>                 print what changed since the last status
  monitor_lib.py record-jobs <state-dir> <context>   store a `kubectl get vcjob -o json` listing from stdin
"""


def main(argv):
    if len(argv) >= 3 and argv[1] == "status":
        only = argv[4:] if len(argv) >= 4 and argv[3] == "--only" else None
        json.dump(status(argv[2], only), sys.stdout, separators=(",", ":"))
        print()
    elif len(argv) >= 2 and argv[1] == "times":
        json.dump(times(), sys.stdout, separators=(",", ":"))
        print()
    elif len(argv) >= 3 and argv[1] == "pending":
        print(" ".join(pending_changes(argv[2])))
    elif len(argv) >= 4 and argv[1] == "record-jobs":
        try:
            record_jobs(argv[2], argv[3], sys.stdin.read())
        except ValueError as error:
            sys.stderr.write("record-jobs: %s\n" % error)
            return 1
    else:
        sys.stderr.write(USAGE)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
