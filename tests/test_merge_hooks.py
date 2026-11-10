"""Tests for merge_hooks.py, which adds the monitor's hook entries to a settings file."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAFETY = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/x/block_force.py"}]}


def merge(path):
    return subprocess.run([sys.executable, os.path.join(ROOT, "merge_hooks.py"), path], capture_output=True, text=True, timeout=10)


class MergeHooks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "settings.json")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text):
        with open(self.path, "w") as f:
            f.write(text)

    def read(self):
        with open(self.path) as f:
            return json.load(f)

    def test_existing_hooks_stay_first_and_the_monitors_are_added(self):
        self.write(json.dumps({"model": "x", "hooks": {"PreToolUse": [SAFETY]}}))
        result = merge(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        settings = self.read()
        self.assertEqual(settings["model"], "x")
        self.assertEqual(settings["hooks"]["PreToolUse"][0], SAFETY)
        self.assertEqual(len(settings["hooks"]["PreToolUse"]), 2)
        with open(os.path.join(ROOT, "settings-hooks.json")) as f:
            wanted = json.load(f)["hooks"]
        for event, groups in wanted.items():
            self.assertEqual(settings["hooks"][event][-1], groups[0], event)

    def test_a_second_run_changes_nothing(self):
        self.write(json.dumps({"hooks": {"PreToolUse": [SAFETY]}}))
        merge(self.path)
        first = self.read()
        result = merge(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read(), first)

    def test_the_original_is_kept_beside_it(self):
        original = json.dumps({"hooks": {"PreToolUse": [SAFETY]}})
        self.write(original)
        merge(self.path)
        merge(self.path)
        with open(self.path + ".before-monitor") as f:
            self.assertEqual(f.read(), original)

    def test_a_settings_file_that_does_not_parse_is_left_alone(self):
        self.write('{"hooks": ')
        result = merge(self.path)
        self.assertNotEqual(result.returncode, 0)
        with open(self.path) as f:
            self.assertEqual(f.read(), '{"hooks": ')
        self.assertFalse(os.path.exists(self.path + ".before-monitor"))

    def test_a_settings_file_with_no_hooks_gets_them(self):
        self.write("{}")
        self.assertEqual(merge(self.path).returncode, 0)
        self.assertEqual(len(self.read()["hooks"]), 6)


if __name__ == "__main__":
    unittest.main()
