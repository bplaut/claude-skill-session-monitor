# Session monitor

A Claude Code skill that watches the desktop app's other sessions and shows
which are waiting on you and what each needs.

## The flow

1. **`hook.py`** runs in every session at six hook events. It appends what the
   session is waiting on (its next-step-box question, a permission prompt) to
   `state/hooks/<session id>.jsonl`.
2. **`poller.sh`** runs in a terminal tab. Every minute it lists your cluster
   jobs into `state/jobs.json` and appends job transitions to
   `state/events.log`.
3. **`watch.py`** runs in the background of the monitor session. It prints a
   line, which wakes the monitor, when a watched hook file or the events log
   grows, and every 20 minutes.
4. **`SKILL.md`** is the `/monitor` procedure the monitor session follows on
   each wake: read the state with `monitor_lib.py status`, decide each
   session's row, and write changed rows to the board.
5. **`board.html`** is the board page. The monitor writes rows into its
   database and the open page redraws itself.

`monitor_lib.py` holds the deterministic parts the scripts share.

## Tests

```bash
/usr/bin/python3 -m unittest discover -s tests
```

They use the system interpreter because the hook runs under it.

## Install

The hook and the poller run outside the sandbox, so they run from a copy that
sandboxed sessions cannot edit. Copy, never symlink:

```bash
mkdir -p ~/.claude/skills/monitor && cp SKILL.md board.html hook.py monitor_lib.py poller.sh watch.py ~/.claude/skills/monitor/
```

Then add the hook entries in `settings-hooks.json` to `~/.claude/settings.json`
(back the file up first, append to arrays that already exist, and check it
still parses).
