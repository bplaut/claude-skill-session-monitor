"""Tests for monitor_lib: hook records, open items, job transitions, status.

Run with the same interpreter the hook uses:
    /usr/bin/python3 -m unittest discover -s tests
"""

import datetime
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


def request(tool="Write", text="plan.md", agent=None):
    return {"kind": "request", "tool_name": tool, "text": text, "agent_id": agent}


def waiting(tool="Write", text="plan.md", agent=None):
    return {"kind": "waiting", "tool_name": tool, "text": text, "agent_id": agent}


def resolved(tool="Write", agent=None):
    return {"kind": "resolved", "tool_name": tool, "agent_id": agent}


class RecordFor(unittest.TestCase):
    def test_box_with_a_question(self):
        record = monitor_lib.record_for(box("Run it again?"), [])
        self.assertEqual(record["kind"], "box")
        self.assertEqual(record["ask"], "Run it again?")
        self.assertEqual(record["actions"], ["Yes", "No"])
        self.assertEqual(record["recap"], "Did the thing.")
        self.assertFalse(record["blocked"])

    def test_box_without_a_question_has_ask_none(self):
        record = monitor_lib.record_for(box(), [])
        self.assertEqual(record["kind"], "box")
        self.assertIsNone(record["ask"])
        self.assertEqual(record["actions"], [])

    def test_other_pretooluse_tools_write_nothing(self):
        event = {"hook_event_name": "PreToolUse", "tool_name": "mcp__ccd_turn__update_plan", "tool_input": {}}
        self.assertIsNone(monitor_lib.record_for(event, []))

    def test_permission_request_keeps_only_the_first_word_of_a_command(self):
        event = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "tool_input": {"command": "HF_TOKEN=abc123 uv run publish data/x"},
        }
        record = monitor_lib.record_for(event, [])
        self.assertEqual(record["kind"], "request")
        self.assertEqual(record["tool_name"], "Bash")
        self.assertEqual(record["text"], "uv")
        self.assertIsNone(record["agent_id"])
        self.assertNotIn("abc123", json.dumps(record))

    def test_permission_request_for_a_file_tool_names_the_file(self):
        event = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Write",
            "tool_input": {"file_path": "/Users/x/notes/plan.md", "content": "secret body"},
        }
        record = monitor_lib.record_for(event, [])
        self.assertEqual(record["text"], "plan.md")
        self.assertNotIn("secret body", json.dumps(record))

    def test_question_dialog_request_carries_its_first_question(self):
        event = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "Which cue?", "options": []}, {"question": "Second?"}]},
        }
        self.assertEqual(monitor_lib.record_for(event, [])["text"], "Which cue?")

    def test_odd_tool_input_shapes_do_not_raise(self):
        for tool_input in (None, "text", [], {"questions": []}, {"questions": ["x"]}, {"command": None}):
            event = {"hook_event_name": "PermissionRequest", "tool_name": "AskUserQuestion", "tool_input": tool_input}
            self.assertEqual(monitor_lib.record_for(event, [])["text"], "", tool_input)

    def test_waiting_takes_its_text_from_the_latest_request(self):
        event = {"hook_event_name": "Notification", "notification_type": "permission_prompt"}
        recent = [request("Bash", "ls"), request("Write", "plan.md", agent="a1")]
        record = monitor_lib.record_for(event, recent)
        self.assertEqual((record["kind"], record["tool_name"], record["text"], record["agent_id"]), ("waiting", "Write", "plan.md", "a1"))

    def test_other_notifications_write_nothing(self):
        idle = {"hook_event_name": "Notification", "notification_type": "idle_prompt"}
        self.assertIsNone(monitor_lib.record_for(idle, [request()]))

    def test_a_tool_call_resolves_only_the_prompt_it_answers(self):
        open_prompt = [request("Write", agent="a1"), waiting("Write", agent="a1")]
        same = {"hook_event_name": "PostToolUse", "tool_name": "Write", "agent_id": "a1"}
        record = monitor_lib.record_for(same, open_prompt)
        self.assertEqual((record["kind"], record["tool_name"], record["agent_id"]), ("resolved", "Write", "a1"))
        other_agent = {"hook_event_name": "PostToolUse", "tool_name": "Write", "agent_id": "b2"}
        main_thread = {"hook_event_name": "PostToolUse", "tool_name": "Write"}
        other_tool = {"hook_event_name": "PostToolUse", "tool_name": "Read", "agent_id": "a1"}
        for event in (other_agent, main_thread, other_tool):
            self.assertIsNone(monitor_lib.record_for(event, open_prompt), event)

    def test_a_tool_call_with_no_open_prompt_writes_nothing(self):
        event = {"hook_event_name": "PostToolUse", "tool_name": "Write"}
        self.assertIsNone(monitor_lib.record_for(event, []))
        self.assertIsNone(monitor_lib.record_for(event, [request()]))
        self.assertIsNone(monitor_lib.record_for(event, [request(), waiting(), resolved()]))

    def test_stop_and_reply(self):
        self.assertEqual(monitor_lib.record_for({"hook_event_name": "Stop"}, [])["kind"], "stop")
        reply = {"hook_event_name": "UserPromptSubmit", "prompt": "yes, go ahead"}
        self.assertEqual(monitor_lib.record_for(reply, [])["kind"], "reply")

    def test_an_automated_or_empty_prompt_starts_a_turn_but_is_not_a_reply(self):
        for prompt in (
            "<task-notification>\n<task-id>b0</task-id>",
            "[SYSTEM NOTIFICATION - NOT USER INPUT]\nThis is an automated",
            "  <task-notification>",
            "",
            None,
        ):
            event = {"hook_event_name": "UserPromptSubmit", "prompt": prompt}
            self.assertEqual(monitor_lib.record_for(event, []), {"kind": "auto"}, prompt)

    def test_a_subagent_writes_only_prompt_records(self):
        self.assertIsNone(monitor_lib.record_for(dict(box("Q?"), agent_id="a1"), []))
        self.assertIsNone(monitor_lib.record_for({"hook_event_name": "Stop", "agent_id": "a1"}, []))
        self.assertIsNone(monitor_lib.record_for({"hook_event_name": "UserPromptSubmit", "prompt": "x", "agent_id": "a1"}, []))
        event = {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "ls"}, "agent_id": "a1"}
        self.assertEqual(monitor_lib.record_for(event, [])["agent_id"], "a1")

    def test_unknown_events_write_nothing(self):
        self.assertIsNone(monitor_lib.record_for({"hook_event_name": "SessionStart"}, []))
        self.assertIsNone(monitor_lib.record_for({}, []))


class EncodeRecord(unittest.TestCase):
    def test_a_long_recap_is_cut(self):
        line = monitor_lib.encode_record({"kind": "box", "ask": "Go?", "actions": ["Yes"], "recap": "x" * 20000})
        self.assertLessEqual(len(line), monitor_lib.RECORD_LIMIT)
        self.assertEqual(json.loads(line)["ask"], "Go?")

    def test_text_that_escapes_to_many_bytes_still_fits(self):
        record = {"kind": "box", "ask": "\U0001f600" * 300, "actions": ["\U0001f600" * 60] * 2, "recap": "\U0001f600" * 500}
        line = monitor_lib.encode_record(record)
        self.assertLessEqual(len(line), monitor_lib.RECORD_LIMIT)
        self.assertEqual(json.loads(line)["kind"], "box")


class OpenItems(unittest.TestCase):
    def items(self, *records):
        return monitor_lib.open_items([dict(r, ts="t%d" % i) for i, r in enumerate(records)])

    def test_a_question_is_open_until_a_reply(self):
        asked = {"kind": "box", "ask": "Go?", "actions": ["Yes"], "recap": "r", "blocked": False}
        self.assertEqual(self.items(asked, {"kind": "stop"})["question"]["ask"], "Go?")
        self.assertIsNone(self.items(asked, {"kind": "stop"}, {"kind": "reply"})["question"])

    def test_a_later_box_without_a_question_replaces_the_old_one(self):
        asked = {"kind": "box", "ask": "Go?", "actions": [], "recap": "r", "blocked": False}
        quiet = {"kind": "box", "ask": None, "actions": [], "recap": "r2", "blocked": False}
        self.assertIsNone(self.items(asked, {"kind": "stop"}, quiet, {"kind": "stop"})["question"])

    def test_a_blocked_box_counts_as_a_question(self):
        blocked = {"kind": "box", "ask": None, "actions": [], "recap": "Stuck until you decide.", "blocked": True}
        self.assertEqual(self.items(blocked)["question"]["recap"], "Stuck until you decide.")

    def test_a_prompt_stays_open_through_other_traffic(self):
        prompt = self.items(request(agent="a1"), waiting(agent="a1"), request("Bash", "ls", agent="b2"), box())["prompt"]
        self.assertEqual((prompt["tool_name"], prompt["text"], prompt["ts"]), ("Write", "plan.md", "t1"))

    def test_a_prompt_closes_on_its_own_resolution_a_turn_end_or_a_reply(self):
        opened = (request(agent="a1"), waiting(agent="a1"))
        self.assertIsNone(self.items(*opened, resolved(agent="a1"))["prompt"])
        self.assertIsNone(self.items(*opened, {"kind": "stop"})["prompt"])
        self.assertIsNone(self.items(*opened, {"kind": "reply"})["prompt"])
        self.assertIsNotNone(self.items(*opened, resolved(agent="b2"))["prompt"])
        self.assertIsNotNone(self.items(*opened, resolved("Read", agent="a1"))["prompt"])

    def test_a_request_alone_is_not_a_prompt(self):
        self.assertIsNone(self.items(request())["prompt"])

    def test_an_automated_turn_leaves_the_question_open(self):
        asked = {"kind": "box", "ask": "Go?", "actions": [], "recap": "r", "blocked": False}
        items = self.items({"kind": "reply"}, asked, {"kind": "stop"}, {"kind": "auto"}, {"kind": "stop"})
        self.assertEqual(items["question"]["ask"], "Go?")

    def test_the_turn_is_in_flight_from_a_prompt_until_the_stop(self):
        asked = {"kind": "box", "ask": "Go?", "actions": [], "recap": "r", "blocked": False}
        self.assertEqual(self.items({"kind": "reply"})["turn"], {"state": "in_flight", "since": "t0"})
        self.assertEqual(self.items({"kind": "reply"}, request(), waiting())["turn"], {"state": "in_flight", "since": "t0"})
        self.assertEqual(self.items({"kind": "reply"}, asked, {"kind": "stop"})["turn"], {"state": "ended", "since": "t2"})
        self.assertEqual(self.items({"kind": "reply"}, asked, {"kind": "stop"}, {"kind": "auto"})["turn"], {"state": "in_flight", "since": "t3"})

    def test_the_turn_is_unknown_without_a_prompt_or_stop(self):
        self.assertIsNone(self.items(request())["turn"])

    def test_no_records(self):
        self.assertEqual(monitor_lib.open_items([]), {"question": None, "prompt": None, "turn": None})


class DiffJobs(unittest.TestCase):
    def test_transitions(self):
        before = {"a": "Running", "b": "Running", "c": "Pending", "d": "Completed", "e": "Running"}
        after = {"a": "Completed", "b": "Failed", "c": "Running", "e": "Running", "f": "Pending"}
        self.assertEqual(
            sorted(monitor_lib.diff_jobs(before, after)),
            [("DONE", "a"), ("FAILED", "b"), ("NEW", "f")],
        )

    def test_an_ended_name_that_comes_back_alive_is_a_new_job(self):
        self.assertEqual(monitor_lib.diff_jobs({"a": "Completed", "b": "Gone"}, {"a": "Pending", "b": "Running"}), [("NEW", "a"), ("NEW", "b")])

    def test_a_running_job_that_vanishes_is_gone(self):
        self.assertEqual(monitor_lib.diff_jobs({"a": "Running"}, {}), [("GONE", "a")])

    def test_a_finished_job_that_vanishes_is_not_reported_again(self):
        self.assertEqual(monitor_lib.diff_jobs({"a": "Completed", "b": "Failed", "c": "Gone"}, {}), [])

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

    def jobs(self, state):
        with open(os.path.join(state, "jobs.json")) as f:
            return json.load(f)["clusters"]["ctx"]

    def test_a_restart_reports_jobs_that_finished_while_it_was_down(self):
        with tempfile.TemporaryDirectory() as state:
            monitor_lib.record_jobs(state, "ctx", self.listing(a="Running", b="Running"), now="2026-11-10T10:00:00-05:00")
            monitor_lib.record_jobs(state, "ctx", self.listing(a="Completed"), now="2026-11-10T10:05:00-05:00")
            with open(os.path.join(state, "events.log")) as log:
                lines = [line.split()[1:] for line in log]
            self.assertIn(["DONE", "ctx", "a"], lines)
            self.assertIn(["GONE", "ctx", "b"], lines)
            self.assertEqual(self.jobs(state)["okAt"], "2026-11-10T10:05:00-05:00")

    def test_an_ended_job_stays_listed_with_its_outcome_after_the_cluster_drops_it(self):
        with tempfile.TemporaryDirectory() as state:
            monitor_lib.record_jobs(state, "ctx", self.listing(a="Running", b="Running"), now="2026-11-10T10:00:00-05:00")
            monitor_lib.record_jobs(state, "ctx", self.listing(a="Completed"), now="2026-11-10T10:05:00-05:00")
            monitor_lib.record_jobs(state, "ctx", self.listing(), now="2026-11-10T10:20:00-05:00")
            jobs = self.jobs(state)["jobs"]
            self.assertEqual((jobs["a"]["phase"], jobs["a"]["endedAt"]), ("Completed", "2026-11-10T10:05:00-05:00"))
            self.assertEqual((jobs["b"]["phase"], jobs["b"]["endedAt"]), ("Gone", "2026-11-10T10:05:00-05:00"))
            with open(os.path.join(state, "events.log")) as log:
                self.assertEqual(len(log.readlines()), 2)

    def test_an_ended_job_is_forgotten_after_a_day(self):
        with tempfile.TemporaryDirectory() as state:
            monitor_lib.record_jobs(state, "ctx", self.listing(a="Running"), now="2026-11-10T10:00:00-05:00")
            monitor_lib.record_jobs(state, "ctx", self.listing(), now="2026-11-10T10:05:00-05:00")
            monitor_lib.record_jobs(state, "ctx", self.listing(), now="2026-11-11T10:04:00-05:00")
            self.assertIn("a", self.jobs(state)["jobs"])
            monitor_lib.record_jobs(state, "ctx", self.listing(), now="2026-11-11T10:06:00-05:00")
            self.assertEqual(self.jobs(state)["jobs"], {})

    def test_the_first_listing_reports_nothing_as_new(self):
        with tempfile.TemporaryDirectory() as state:
            monitor_lib.record_jobs(state, "ctx", self.listing(a="Running"), now="2026-11-10T10:00:00-05:00")
            self.assertFalse(os.path.exists(os.path.join(state, "events.log")))

    def test_bad_kubectl_output_leaves_ok_at_unchanged(self):
        with tempfile.TemporaryDirectory() as state:
            monitor_lib.record_jobs(state, "ctx", self.listing(a="Running"), now="2026-11-10T10:00:00-05:00")
            for bad in ("error: You must be logged in", "[]", '{"items": [{"metadata": {}}]}'):
                with self.assertRaises(ValueError):
                    monitor_lib.record_jobs(state, "ctx", bad, now="2026-11-10T10:05:00-05:00")
            self.assertEqual(self.jobs(state)["okAt"], "2026-11-10T10:00:00-05:00")


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

    def test_each_session_reports_how_long_ago_its_turn_started_or_ended(self):
        with tempfile.TemporaryDirectory() as state:
            ended = (datetime.datetime.now().astimezone() - datetime.timedelta(seconds=90)).isoformat(timespec="seconds")
            self.write(state, "local_a", {"ts": ended, "kind": "stop"})
            self.write(state, "local_b", {"ts": "not a time", "kind": "reply"})
            sessions = monitor_lib.status(state)["sessions"]
            self.assertEqual(sessions["local_a"]["turn"]["state"], "ended")
            self.assertTrue(85 <= sessions["local_a"]["turn"]["age_seconds"] <= 100, sessions["local_a"]["turn"])
            self.assertEqual(sessions["local_b"]["turn"], {"state": "in_flight", "since": "not a time", "age_seconds": None})

    def test_job_events_are_reported_once(self):
        with tempfile.TemporaryDirectory() as state:
            with open(os.path.join(state, "events.log"), "w") as f:
                f.write("2026-11-10T10:05:00-05:00 DONE ctx job-a\n")
            expected = [{"ts": "2026-11-10T10:05:00-05:00", "event": "DONE", "cluster": "ctx", "job": "job-a"}]
            self.assertEqual(monitor_lib.status(state)["job_events"], expected)
            self.assertEqual(monitor_lib.status(state)["job_events"], [])

    def test_an_events_log_that_was_replaced_by_a_shorter_one_is_read_from_its_start(self):
        with tempfile.TemporaryDirectory() as state:
            path = os.path.join(state, "events.log")
            with open(path, "w") as f:
                f.write("2026-11-10T10:05:00-05:00 DONE ctx job-a\n2026-11-10T10:06:00-05:00 DONE ctx job-b\n")
            monitor_lib.status(state)
            with open(path, "w") as f:
                f.write("2026-11-10T11:00:00-05:00 FAILED ctx job-c\n")
            self.assertEqual(monitor_lib.pending_changes(state), ["jobs"])
            self.assertEqual([e["job"] for e in monitor_lib.status(state)["job_events"]], ["job-c"])

    def test_the_times_to_write_on_the_board_come_ready_made(self):
        with tempfile.TemporaryDirectory() as state:
            report = monitor_lib.status(state)
            now = datetime.datetime.fromisoformat(report["now"])
            next_by = datetime.datetime.fromisoformat(report["next_by"])
            self.assertIsNotNone(now.utcoffset())
            self.assertEqual(next_by - now, datetime.timedelta(minutes=25))
            self.assertEqual(report["checked_at"], now.strftime("%H:%M"))
            self.assertIsNone(report["cluster_ok_at"])

    def test_times_gives_the_stamp_values_without_marking_anything_seen(self):
        with tempfile.TemporaryDirectory() as state:
            self.write(state, "local_a", {"ts": "t1", "kind": "stop"})
            times = monitor_lib.times()
            self.assertEqual(sorted(times), ["checked_at", "next_by", "now"])
            self.assertEqual(monitor_lib.pending_changes(state), ["local_a"])

    def test_cluster_ok_at_is_the_newest_successful_read(self):
        with tempfile.TemporaryDirectory() as state:
            listing = json.dumps({"items": []})
            monitor_lib.record_jobs(state, "one", listing, now="2026-11-10T10:00:00-05:00")
            monitor_lib.record_jobs(state, "two", listing, now="2026-11-10T10:07:00-05:00")
            self.assertEqual(monitor_lib.status(state)["cluster_ok_at"], "2026-11-10T10:07:00-05:00")

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
                f.write("2026-11-10T10:05:00-05:00 DONE ctx job-a\n")
            self.assertEqual(monitor_lib.pending_changes(state), ["jobs"])


if __name__ == "__main__":
    unittest.main()
