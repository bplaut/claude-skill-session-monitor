"""Tests for the two long-running scripts: the watcher and the poller."""

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import monitor_lib  # noqa: E402


class Watcher:
    """The watcher as a child process, with the lines it has printed so far."""

    def __init__(self, state, poll="0.1", tick="3600"):
        self.lines = []
        self.process = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "watch.py"), state, poll, tick], stdout=subprocess.PIPE, text=True
        )
        self.reader = threading.Thread(target=lambda: self.lines.extend(line.strip() for line in self.process.stdout))
        self.reader.start()

    def wait_for(self, count, seconds=5.0):
        deadline = time.monotonic() + seconds
        while len(self.lines) < count and time.monotonic() < deadline:
            time.sleep(0.05)
        return list(self.lines)

    def stop(self):
        self.process.terminate()
        self.process.wait(timeout=5)
        self.reader.join(timeout=5)
        self.process.stdout.close()
        return list(self.lines)


def append(state, session, kind):
    os.makedirs(os.path.join(state, "hooks"), exist_ok=True)
    with open(os.path.join(state, "hooks", session + ".jsonl"), "a") as f:
        f.write(json.dumps({"ts": "t", "kind": kind}) + "\n")


class Watch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.watchers = []

    def tearDown(self):
        for watcher in self.watchers:
            if watcher.process.poll() is None:
                watcher.stop()
        self.tmp.cleanup()

    def watch(self, **options):
        watcher = Watcher(self.state, **options)
        self.watchers.append(watcher)
        return watcher

    def test_a_grown_hook_file_is_announced_once(self):
        append(self.state, "local_a", "stop")
        watcher = self.watch()
        watcher.wait_for(1)
        time.sleep(0.5)
        self.assertEqual(watcher.stop(), ["CHANGED local_a"])

    def test_the_same_file_growing_again_after_a_read_is_announced_again(self):
        append(self.state, "local_a", "stop")
        watcher = self.watch()
        self.assertEqual(watcher.wait_for(1), ["CHANGED local_a"])
        monitor_lib.status(self.state)
        append(self.state, "local_a", "stop")
        self.assertEqual(watcher.wait_for(2), ["CHANGED local_a", "CHANGED local_a"])

    def test_nothing_pending_prints_nothing(self):
        watcher = self.watch()
        time.sleep(0.5)
        self.assertEqual(watcher.stop(), [])

    def test_ticks_on_its_timer(self):
        watcher = self.watch(tick="0.3")
        self.assertIn("TICK", watcher.wait_for(1))


class Poller(unittest.TestCase):
    def script(self):
        with open(os.path.join(ROOT, "poller.sh")) as f:
            return "\n".join(line for line in f.read().splitlines() if not line.lstrip().startswith("#"))

    def test_every_kubectl_call_is_read_only(self):
        script = self.script()
        calls = [script[m.start() :].splitlines()[0] for m in re.finditer(r"\bkubectl\b", script)]
        self.assertTrue(calls)
        allowed = re.compile(r'kubectl (config view --raw --flatten --minify --context "\$ctx" >|--kubeconfig "\$KDIR/\$ctx" -n u-eval-awareness --request-timeout=20s get vcjob -l "username=\$FLAMINGO_USERNAME" -o json 2>)')
        for call in calls:
            self.assertRegex(call, allowed)
        self.assertNotRegex(script, r"\b(delete|apply|create|patch|edit|replace|exec|scale|cp)\b")

    def test_it_never_forces_a_delete(self):
        self.assertNotRegex(self.script(), r"\brm\s+-\w*[rf]")

    def test_it_refuses_to_run_without_the_cluster_username(self):
        env = {k: v for k, v in os.environ.items() if k != "FLAMINGO_USERNAME"}
        with tempfile.TemporaryDirectory() as state:
            result = subprocess.run(["bash", os.path.join(ROOT, "poller.sh"), state], env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FLAMINGO_USERNAME", result.stderr)

    def test_the_loop_survives_failed_reads_and_cleans_up_its_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir, state, scratch = (os.path.join(tmp, name) for name in ("bin", "state", "scratch"))
            for directory in (bin_dir, scratch):
                os.makedirs(directory)
            # kubectl: "good" lists one job, "badjson" prints text, "down" fails.
            self.stub(bin_dir, "kubectl", """
case "$*" in
  *"config view"*) echo "fake kubeconfig" ;;
  *KDIR_good*|*/good\\ *) echo '{"items": [{"metadata": {"name": "job-a"}, "status": {"state": {"phase": "Running"}}}]}' ;;
  */badjson\\ *) echo "not json" ;;
  *) echo "Unable to connect to the server" >&2; exit 1 ;;
esac
""")
            # sleep: let two rounds run, then stop the poller the way Ctrl-C would.
            self.stub(bin_dir, "sleep", """
count_file="%s/rounds"
n=$(cat "$count_file" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$count_file"
[ "$n" -ge 2 ] && kill -TERM "$PPID"
exit 0
""" % scratch)
            env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ["PATH"], TMPDIR=scratch, FLAMINGO_USERNAME="someone")
            result = subprocess.run(
                ["/bin/bash", os.path.join(ROOT, "poller.sh"), state, "good", "badjson", "down"],
                env=env, capture_output=True, text=True, timeout=30,
            )
            rounds = [line for line in result.stdout.splitlines() if "good ok" in line]
            self.assertEqual(len(rounds), 2, result.stdout + result.stderr)
            self.assertIn("badjson RECORD FAILED", rounds[0])
            self.assertIn("down READ FAILED (Unable to connect to the server", rounds[0])
            self.assertNotIn("Traceback", result.stdout + result.stderr)
            self.assertEqual([name for name in os.listdir(scratch) if name.startswith("monitor-kube")], [])
            with open(os.path.join(state, "jobs.json")) as f:
                self.assertEqual(list(json.load(f)["clusters"]), ["good"])

    def stub(self, directory, name, body):
        path = os.path.join(directory, name)
        with open(path, "w") as f:
            f.write("#!/bin/bash\n" + body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
