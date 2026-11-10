"""Tests for the two long-running scripts: the watcher and the poller."""

import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def watch(state, seconds, poll="0.1", tick="3600"):
    """Run the watcher for a moment and return the lines it printed."""
    process = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "watch.py"), state, poll, tick],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        process.terminate()
    out, _ = process.communicate(timeout=5)
    return out.splitlines()


class Watch(unittest.TestCase):
    def test_a_grown_hook_file_is_announced_once(self):
        with tempfile.TemporaryDirectory() as state:
            os.makedirs(os.path.join(state, "hooks"))
            with open(os.path.join(state, "hooks", "local_a.jsonl"), "w") as f:
                f.write('{"ts": "t1", "kind": "stop"}\n')
            self.assertEqual(watch(state, 0.8), ["CHANGED local_a"])

    def test_nothing_pending_prints_nothing(self):
        with tempfile.TemporaryDirectory() as state:
            self.assertEqual(watch(state, 0.5), [])

    def test_ticks_on_its_timer(self):
        with tempfile.TemporaryDirectory() as state:
            self.assertIn("TICK", watch(state, 1.0, tick="0.3"))


class Poller(unittest.TestCase):
    def test_every_kubectl_call_is_read_only(self):
        with open(os.path.join(ROOT, "poller.sh")) as f:
            calls = [line.strip() for line in f if re.search(r"\bkubectl\b", line) and not line.lstrip().startswith("#")]
        self.assertTrue(calls)
        for call in calls:
            self.assertRegex(call, r"kubectl (config view|--kubeconfig \S+ -n u-eval-awareness --request-timeout=\S+ get vcjob)\b", call)

    def test_it_never_forces_a_delete(self):
        with open(os.path.join(ROOT, "poller.sh")) as f:
            script = f.read()
        self.assertNotRegex(script, r"\brm\s+-\w*[rf]")

    def test_it_refuses_to_run_without_the_cluster_username(self):
        env = {k: v for k, v in os.environ.items() if k != "FLAMINGO_USERNAME"}
        with tempfile.TemporaryDirectory() as state:
            result = subprocess.run(["bash", os.path.join(ROOT, "poller.sh"), state], env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FLAMINGO_USERNAME", result.stderr)


if __name__ == "__main__":
    unittest.main()
