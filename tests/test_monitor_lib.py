"""Tests for monitor_lib: hook records, open items, job transitions, status.

Run with the same interpreter the hook uses:
    /usr/bin/python3 -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import monitor_lib  # noqa: E402


def box(ask=None, **extra):
    tool_input = {"recap": "Did the thing."}
    if ask is not None:
        tool_input["ask"] = ask
        tool_input["actions"] = [{"label": "Yes"}, {"label": "No", "action": "x"}]
    tool_input.update(extra)
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "mcp__ccd_turn__wrap_up",
        "tool_use_id": "toolu_1",
        "tool_input": tool_input,
    }


class RecordFor(unittest.TestCase):
    def test_box_with_a_question(self):
        record = monitor_lib.record_for(box("Run it again?"), last_kind=None)
        self.assertEqual(record["kind"], "box")
        self.assertEqual(record["ask"], "Run it again?")
        self.assertEqual(record["actions"], ["Yes", "No"])
        self.assertEqual(record["recap"], "Did the thing.")
        self.assertFalse(record["blocked"])

    def test_box_without_a_question_has_ask_none(self):
        record = monitor_lib.record_for(box(), last_kind=None)
        self.assertEqual(record["kind"], "box")
        self.assertIsNone(record["ask"])
        self.assertEqual(record["actions"], [])

    def test_other_pretooluse_tools_write_nothing(self):
        event = {"hook_event_name": "PreToolUse", "tool_name": "mcp__ccd_turn__update_plan", "tool_input": {}}
        self.assertIsNone(monitor_lib.record_for(event, last_kind=None))

    def test_permission_request_keeps_only_the_first_word_of_a_command(self):
        event = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "tool_input": {"command": "HF_TOKEN=abc123 uv run publish data/x"},
        }
        record = monitor_lib.record_for(event, last_kind=None)
        self.assertEqual(record["kind"], "request")
        self.assertEqual(record["tool_name"], "Bash")
        self.assertEqual(record["text"], "uv")
        self.assertNotIn("abc123", json.dumps(record))

    def test_permission_request_for_a_file_tool_names_the_file(self):
        event = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Write",
            "tool_input": {"file_path": "/Users/x/notes/plan.md", "content": "secret body"},
        }
        record = monitor_lib.record_for(event, last_kind=None)
        self.assertEqual(record["text"], "plan.md")
        self.assertNotIn("secret body", json.dumps(record))

    def test_question_dialog_request_carries_its_first_question(self):
        event = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "Which cue?", "options": []}, {"question": "Second?"}]},
        }
        record = monitor_lib.record_for(event, last_kind=None)
        self.assertEqual(record["text"], "Which cue?")

    def test_only_the_permission_prompt_notification_is_waiting(self):
        waiting = {"hook_event_name": "Notification", "notification_type": "permission_prompt"}
        idle = {"hook_event_name": "Notification", "notification_type": "idle_prompt"}
        self.assertEqual(monitor_lib.record_for(waiting, last_kind="request")["kind"], "waiting")
        self.assertIsNone(monitor_lib.record_for(idle, last_kind="stop"))

    def test_posttooluse_resolves_only_an_open_prompt(self):
        event = {"hook_event_name": "PostToolUse", "tool_name": "Write"}
        self.assertEqual(monitor_lib.record_for(event, last_kind="waiting")["kind"], "resolved")
        self.assertIsNone(monitor_lib.record_for(event, last_kind="request"))
        self.assertIsNone(monitor_lib.record_for(event, last_kind=None))

    def test_stop_and_reply(self):
        self.assertEqual(monitor_lib.record_for({"hook_event_name": "Stop"}, None)["kind"], "stop")
        reply = {"hook_event_name": "UserPromptSubmit", "prompt": "yes, go ahead"}
        self.assertEqual(monitor_lib.record_for(reply, None)["kind"], "reply")

    def test_an_automated_prompt_is_not_a_reply(self):
        for prompt in (
            "<task-notification>\n<task-id>b0</task-id>",
            "[SYSTEM NOTIFICATION - NOT USER INPUT]\nThis is an automated",
            "  <task-notification>",
        ):
            event = {"hook_event_name": "UserPromptSubmit", "prompt": prompt}
            self.assertIsNone(monitor_lib.record_for(event, None), prompt)

    def test_a_subagent_writes_only_prompt_records(self):
        self.assertIsNone(monitor_lib.record_for(dict(box("Q?"), agent_id="a1"), None))
        self.assertIsNone(monitor_lib.record_for({"hook_event_name": "Stop", "agent_id": "a1"}, None))
        request = {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "ls"}, "agent_id": "a1"}
        self.assertEqual(monitor_lib.record_for(request, None)["kind"], "request")
        resolved = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "agent_id": "a1"}
        self.assertEqual(monitor_lib.record_for(resolved, "waiting")["kind"], "resolved")

    def test_unknown_events_write_nothing(self):
        self.assertIsNone(monitor_lib.record_for({"hook_event_name": "SessionStart"}, None))
        self.assertIsNone(monitor_lib.record_for({}, None))


class OpenItems(unittest.TestCase):
    def kinds(self, *records):
        return monitor_lib.open_items([dict(r, ts="t%d" % i) for i, r in enumerate(records)])

    def test_a_question_is_open_until_a_reply(self):
        asked = {"kind": "box", "ask": "Go?", "actions": ["Yes"], "recap": "r", "blocked": False}
        self.assertEqual(self.kinds(asked, {"kind": "stop"})["question"]["ask"], "Go?")
        self.assertIsNone(self.kinds(asked, {"kind": "stop"}, {"kind": "reply"})["question"])

    def test_a_later_box_without_a_question_replaces_the_old_one(self):
        asked = {"kind": "box", "ask": "Go?", "actions": [], "recap": "r", "blocked": False}
        quiet = {"kind": "box", "ask": None, "actions": [], "recap": "r2", "blocked": False}
        self.assertIsNone(self.kinds(asked, {"kind": "stop"}, quiet, {"kind": "stop"})["question"])

    def test_a_blocked_box_counts_as_a_question(self):
        blocked = {"kind": "box", "ask": None, "actions": [], "recap": "Stuck until you decide.", "blocked": True}
        self.assertEqual(self.kinds(blocked)["question"]["recap"], "Stuck until you decide.")

    def test_a_prompt_is_open_only_while_waiting_is_the_last_record(self):
        request = {"kind": "request", "tool_name": "Write", "text": "plan.md"}
        open_now = self.kinds(request, {"kind": "waiting"})
        self.assertEqual(open_now["prompt"]["tool_name"], "Write")
        self.assertEqual(open_now["prompt"]["text"], "plan.md")
        self.assertIsNone(self.kinds(request, {"kind": "waiting"}, {"kind": "resolved"})["prompt"])
        self.assertIsNone(self.kinds(request)["prompt"])

    def test_no_records(self):
        self.assertEqual(monitor_lib.open_items([]), {"question": None, "prompt": None})


class DiffJobs(unittest.TestCase):
    def test_transitions(self):
        before = {"a": "Running", "b": "Running", "c": "Pending", "d": "Completed", "e": "Running"}
        after = {"a": "Completed", "b": "Failed", "c": "Running", "e": "Running", "f": "Pending"}
        self.assertEqual(
            sorted(monitor_lib.diff_jobs(before, after)),
            [("DONE", "a"), ("FAILED", "b"), ("NEW", "f")],
        )

    def test_a_running_job_that_vanishes_is_gone(self):
        self.assertEqual(monitor_lib.diff_jobs({"a": "Running"}, {}), [("GONE", "a")])

    def test_a_finished_job_that_vanishes_is_not_reported_again(self):
        self.assertEqual(monitor_lib.diff_jobs({"a": "Completed", "b": "Failed"}, {}), [])

    def test_nothing_changes(self):
        self.assertEqual(monitor_lib.diff_jobs({"a": "Running"}, {"a": "Running"}), [])
        self.assertEqual(monitor_lib.diff_jobs({}, {}), [])


class RecordJobs(unittest.TestCase):
    def listing(self, **phases):
        items = [
            {"metadata": {"name": n, "creationTimestamp": "2026-11-10T10:00:00Z"}, "status": {"state": {"phase": p}}}
            for n, p in phases.items()
        ]
        return json.dumps({"items": items})

    def test_a_restart_reports_jobs_that_finished_while_it_was_down(self):
        with tempfile.TemporaryDirectory() as state:
            monitor_lib.record_jobs(state, "ctx", self.listing(a="Running", b="Running"), now="2026-11-10T10:00:00")
            monitor_lib.record_jobs(state, "ctx", self.listing(a="Completed"), now="2026-11-10T10:05:00")
            with open(os.path.join(state, "events.log")) as log:
                lines = [line.split()[1:] for line in log]
            self.assertIn(["DONE", "ctx", "a"], lines)
            self.assertIn(["GONE", "ctx", "b"], lines)
            with open(os.path.join(state, "jobs.json")) as f:
                jobs = json.load(f)
            self.assertEqual(jobs["clusters"]["ctx"]["okAt"], "2026-11-10T10:05:00")
            self.assertEqual(jobs["clusters"]["ctx"]["jobs"]["a"]["phase"], "Completed")

    def test_the_first_listing_reports_nothing_as_new(self):
        with tempfile.TemporaryDirectory() as state:
            monitor_lib.record_jobs(state, "ctx", self.listing(a="Running"), now="2026-11-10T10:00:00")
            self.assertFalse(os.path.exists(os.path.join(state, "events.log")))

    def test_bad_kubectl_output_leaves_ok_at_unchanged(self):
        with tempfile.TemporaryDirectory() as state:
            monitor_lib.record_jobs(state, "ctx", self.listing(a="Running"), now="2026-11-10T10:00:00")
            with self.assertRaises(ValueError):
                monitor_lib.record_jobs(state, "ctx", "error: You must be logged in", now="2026-11-10T10:05:00")
            with open(os.path.join(state, "jobs.json")) as f:
                self.assertEqual(json.load(f)["clusters"]["ctx"]["okAt"], "2026-11-10T10:00:00")


class Status(unittest.TestCase):
    def write(self, state, session, *records):
        os.makedirs(os.path.join(state, "hooks"), exist_ok=True)
        with open(os.path.join(state, "hooks", session + ".jsonl"), "a") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")

    def test_changed_only_once_and_half_written_lines_wait(self):
        with tempfile.TemporaryDirectory() as state:
            self.write(state, "local_a", {"ts": "t1", "kind": "box", "ask": "Go?", "actions": [], "recap": "r", "blocked": False})
            with open(os.path.join(state, "hooks", "local_a.jsonl"), "a") as f:
                f.write('{"ts": "t2", "kind": "st')  # the hook is mid-write
            first = monitor_lib.status(state)
            self.assertTrue(first["sessions"]["local_a"]["changed"])
            self.assertEqual(first["sessions"]["local_a"]["question"]["ask"], "Go?")
            second = monitor_lib.status(state)
            self.assertFalse(second["sessions"]["local_a"]["changed"])
            self.assertEqual(second["sessions"]["local_a"]["question"]["ask"], "Go?")
            with open(os.path.join(state, "hooks", "local_a.jsonl"), "a") as f:
                f.write('op"}\n{"ts": "t3", "kind": "reply"}\n')
            third = monitor_lib.status(state)
            self.assertTrue(third["sessions"]["local_a"]["changed"])
            self.assertIsNone(third["sessions"]["local_a"]["question"])

    def test_job_events_are_reported_once(self):
        with tempfile.TemporaryDirectory() as state:
            with open(os.path.join(state, "events.log"), "w") as f:
                f.write("2026-11-10T10:05:00 DONE ctx job-a\n")
            self.assertEqual(monitor_lib.status(state)["job_events"], [{"ts": "2026-11-10T10:05:00", "event": "DONE", "cluster": "ctx", "job": "job-a"}])
            self.assertEqual(monitor_lib.status(state)["job_events"], [])

    def test_only_the_named_sessions_are_reported_and_watched(self):
        with tempfile.TemporaryDirectory() as state:
            self.write(state, "local_a", {"ts": "t1", "kind": "stop"})
            self.write(state, "local_b", {"ts": "t1", "kind": "stop"})
            report = monitor_lib.status(state, only=["local_a"])
            self.assertEqual(list(report["sessions"]), ["local_a"])
            # A session outside the list still has its records marked seen, and
            # its later turns no longer count as something to wake up for.
            self.write(state, "local_b", {"ts": "t2", "kind": "stop"})
            self.assertEqual(monitor_lib.pending_changes(state), [])
            self.write(state, "local_a", {"ts": "t2", "kind": "stop"})
            self.assertEqual(monitor_lib.pending_changes(state), ["local_a"])

    def test_an_empty_list_watches_no_session(self):
        with tempfile.TemporaryDirectory() as state:
            self.write(state, "local_a", {"ts": "t1", "kind": "stop"})
            self.assertEqual(monitor_lib.status(state, only=[])["sessions"], {})
            self.write(state, "local_a", {"ts": "t2", "kind": "stop"})
            self.assertEqual(monitor_lib.pending_changes(state), [])

    def test_files_with_bad_names_are_ignored(self):
        with tempfile.TemporaryDirectory() as state:
            self.write(state, "not-a-session", {"ts": "t1", "kind": "stop"})
            self.assertEqual(monitor_lib.status(state)["sessions"], {})


class PendingChanges(unittest.TestCase):
    def test_reports_growth_past_the_stored_offsets_without_moving_them(self):
        with tempfile.TemporaryDirectory() as state:
            os.makedirs(os.path.join(state, "hooks"))
            with open(os.path.join(state, "hooks", "local_a.jsonl"), "w") as f:
                f.write('{"ts": "t1", "kind": "stop"}\n')
            self.assertEqual(monitor_lib.pending_changes(state), ["local_a"])
            self.assertEqual(monitor_lib.pending_changes(state), ["local_a"])
            monitor_lib.status(state)
            self.assertEqual(monitor_lib.pending_changes(state), [])
            with open(os.path.join(state, "events.log"), "w") as f:
                f.write("2026-11-10T10:05:00 DONE ctx job-a\n")
            self.assertEqual(monitor_lib.pending_changes(state), ["jobs"])


if __name__ == "__main__":
    unittest.main()
