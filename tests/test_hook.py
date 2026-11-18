"""Tests for hook.py as Claude Code runs it: JSON on stdin, state dir as argv."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hook.py")
SESSION = "local_673f2e1e-ef2f-4949-9394-04d38509aed0"


def run_hook(state, event, session=SESSION):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_HOST_SESSION_ID"}
    if session is not None:
        env["CLAUDE_CODE_HOST_SESSION_ID"] = session
    stdin = event if isinstance(event, str) else json.dumps(event)
    return subprocess.run(
        [sys.executable, HOOK, state], input=stdin, env=env, capture_output=True, text=True, timeout=10
    )


def records(state, session=SESSION):
    path = os.path.join(state, "hooks", session + ".jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f]


BOX = {
    "hook_event_name": "PreToolUse",
    "tool_name": "mcp__ccd_turn__wrap_up",
    "tool_use_id": "toolu_1",
    "tool_input": {"recap": "Ran date.", "ask": "Run date again?", "actions": [{"label": "Yes, run it"}]},
}


class Hook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def assert_silent_success(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_a_box_question_is_recorded_under_the_host_session_id(self):
        self.assert_silent_success(run_hook(self.state, BOX))
        (record,) = records(self.state)
        self.assertEqual(record["kind"], "box")
        self.assertEqual(record["ask"], "Run date again?")
        self.assertEqual(record["actions"], ["Yes, run it"])
        self.assertEqual(record["tool_use_id"], "toolu_1")
        self.assertIn("ts", record)
        mode = stat.S_IMODE(os.stat(os.path.join(self.state, "hooks", SESSION + ".jsonl")).st_mode)
        self.assertEqual(mode, 0o600)

    def test_a_prompt_opens_and_a_later_tool_call_resolves_it(self):
        run_hook(self.state, {"hook_event_name": "PermissionRequest", "tool_name": "Write", "tool_input": {"file_path": "/x/y.txt"}})
        run_hook(self.state, {"hook_event_name": "Notification", "notification_type": "permission_prompt", "message": "Claude needs your permission to use Write"})
        self.assert_silent_success(run_hook(self.state, {"hook_event_name": "PostToolUse", "tool_name": "Write"}))
        self.assertEqual([r["kind"] for r in records(self.state)], ["request", "waiting", "resolved"])

    def test_another_agents_tool_call_leaves_the_prompt_open(self):
        run_hook(self.state, {"hook_event_name": "PermissionRequest", "tool_name": "Write", "tool_input": {"file_path": "/x/y.txt"}, "agent_id": "a1"})
        run_hook(self.state, {"hook_event_name": "Notification", "notification_type": "permission_prompt"})
        self.assert_silent_success(run_hook(self.state, {"hook_event_name": "PostToolUse", "tool_name": "Read", "agent_id": "b2"}))
        self.assert_silent_success(run_hook(self.state, {"hook_event_name": "PostToolUse", "tool_name": "Write"}))
        self.assertEqual([r["kind"] for r in records(self.state)], ["request", "waiting"])
        self.assertEqual(records(self.state)[1]["agent_id"], "a1")
        run_hook(self.state, {"hook_event_name": "PostToolUse", "tool_name": "Write", "agent_id": "a1"})
        self.assertEqual(records(self.state)[-1]["kind"], "resolved")

    def test_a_tool_call_with_no_open_prompt_writes_nothing(self):
        run_hook(self.state, BOX)
        self.assert_silent_success(run_hook(self.state, {"hook_event_name": "PostToolUse", "tool_name": "Bash"}))
        self.assertEqual([r["kind"] for r in records(self.state)], ["box"])

    def test_a_tool_call_in_a_session_with_no_file_creates_none(self):
        self.assert_silent_success(run_hook(self.state, {"hook_event_name": "PostToolUse", "tool_name": "Bash"}))
        self.assertFalse(os.path.exists(os.path.join(self.state, "hooks", SESSION + ".jsonl")))

    def test_a_missing_or_malformed_session_id_writes_nothing(self):
        self.assert_silent_success(run_hook(self.state, BOX, session=None))
        self.assert_silent_success(run_hook(self.state, BOX, session="../../etc/passwd"))
        self.assert_silent_success(run_hook(self.state, BOX, session="2998b424-550c-4de7"))
        self.assertFalse(os.path.exists(os.path.join(self.state, "hooks")))

    def test_bad_input_is_silent_and_writes_nothing(self):
        self.assert_silent_success(run_hook(self.state, "not json"))
        self.assert_silent_success(run_hook(self.state, "[1, 2]"))
        self.assertEqual(records(self.state), [])

    def test_a_missing_state_dir_argument_is_silent(self):
        env = dict(os.environ, CLAUDE_CODE_HOST_SESSION_ID=SESSION)
        result = subprocess.run([sys.executable, HOOK], input=json.dumps(BOX), env=env, capture_output=True, text=True, timeout=10)
        self.assert_silent_success(result)

    def test_a_stop_records_the_closing_message(self):
        event = {"hook_event_name": "Stop", "last_assistant_message": "All done.\n\nRun `make`."}
        self.assert_silent_success(run_hook(self.state, event))
        self.assertEqual(records(self.state)[-1]["text"], "All done. Run `make`.")

    def test_a_long_recap_is_cut_so_the_record_stays_small(self):
        event = json.loads(json.dumps(BOX))
        event["tool_input"]["recap"] = "x" * 20000
        self.assert_silent_success(run_hook(self.state, event))
        with open(os.path.join(self.state, "hooks", SESSION + ".jsonl"), "rb") as f:
            line = f.read()
        self.assertLessEqual(len(line), 4096)
        self.assertEqual(json.loads(line)["ask"], "Run date again?")


if __name__ == "__main__":
    unittest.main()
