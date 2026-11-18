# Session monitor

A Claude Code skill for the Claude desktop app. One dedicated session, the
monitor, watches your other sessions and keeps a board page that shows which
are waiting on you and what each needs, which are waiting on a run, and which
are working or idle.

## The flow

1. **`hook.py`** runs in every session at six hook events. It appends what the
   session is waiting on (its next-step-box question, a permission prompt) and
   when its turns start and end to `<state>/hooks/<session id>.jsonl`.
2. **`poller.sh`** runs in a terminal tab. Every minute it lists your cluster
   jobs into `<state>/jobs.json` and appends job transitions to
   `<state>/events.log`.
3. **`watch.py`** runs in the background of the monitor session. It prints a
   line, which wakes the monitor, when a watched hook file or the events log
   grows. The Monitor tool stops it every 30 minutes; the monitor restarts it,
   and also runs a wake if its last one was over 45 minutes ago.
4. **`SKILL.md`** is the `/monitor` procedure the monitor session follows on
   each wake: read the state with `monitor_lib.py status`, decide each
   session's row, write changed rows to the board, and print the board in
   chat when a row changed.
5. **`board.html`** is the board page. The monitor writes rows into its
   database and the open page redraws itself.

`monitor_lib.py` holds the deterministic parts the scripts share.

## Setup

The files in this repo are set up for one machine. Three values are written
into `SKILL.md` and `settings-hooks.json` and have to become yours: the home
folder, the state folder and the board's link. Steps 2 and 3 change them.

You need:

- macOS and the Claude desktop app's Code tab.
- `/usr/bin/python3`, version 3.9 or later (`xcode-select --install` provides it).
- A sidebar group named `Active` in the app. The monitor shows the sessions in
  that group.
- For the poller only: `kubectl` contexts for FAR.AI's Flamingo clusters and
  `FLAMINGO_USERNAME` set in your shell. The poller reads the
  `u-eval-awareness` namespace; change that in `poller.sh` if your jobs run in
  another. Without the poller everything else works, and rows that wait on a
  run say "cluster unread".

### 1. Clone, and pick a state folder

```bash
git clone git@github.com:bplaut/claude-skill-session-monitor.git ~/claude-skill-session-monitor
```

The state folder holds the hook records and the job list. It must be inside
the folder the monitor session will run in, because a session's sandbox can
only write there. If that folder is a git checkout, pick a path the checkout
ignores. The steps below call the session's folder `<project>` and use
`<project>/.session-monitor` as the state folder.

### 2. Point the files at your machine

From the clone, with `<project>` replaced by your folder:

```bash
STATE="$HOME/<project>/.session-monitor"
```

```bash
sed -i '' -e "s#/Users/bplaut/eval-awareness/playground/session-monitor/state#$STATE#g" -e "s#/Users/bplaut#$HOME#g" SKILL.md settings-hooks.json
```

Nothing should be left pointing at the original machine:

```bash
grep -n "bplaut" SKILL.md settings-hooks.json
```

### 3. Publish your own board

The board is a private page on claude.ai with a small database. Open a Claude
Code session in the clone and ask it:

> Publish board.html as a private Artifact with the db and user capabilities,
> and give me its link.

Replace the link on the `Board:` line of `SKILL.md` with yours.

Commit steps 2 and 3 in your clone, so a later `git pull` merges around them.

### 4. Install the skill

The hook and the poller run outside the sandbox, so they run from a copy that
sandboxed sessions cannot edit. Copy, never symlink:

```bash
mkdir -p ~/.claude/skills/monitor && cp SKILL.md board.html hook.py monitor_lib.py poller.sh watch.py ~/.claude/skills/monitor/
```

### 5. Add the hooks

```bash
/usr/bin/python3 merge_hooks.py ~/.claude/settings.json
```

It appends the six entries in `settings-hooks.json` after the hooks you already
have, keeps the previous file as `settings.json.before-monitor`, and changes
nothing if the file does not parse or the entries are already there. Sessions
that are already running pick the hooks up without a restart.

### 6. Start the poller

In a terminal tab, not from a Claude session: the cluster sign-in can only
refresh outside the sandbox. Skip this step if you have no cluster jobs.

```bash
~/.claude/skills/monitor/poller.sh "$HOME/<project>/.session-monitor"
```

It polls three clusters by default; name the contexts you want after the state
folder to poll others.

### 7. Start the monitor

Create a new session in `<project>`, after step 4 so that it sees the skill.
Pin it, put it in auto mode, and leave its title alone: the monitor titles
itself, and a title you set by hand makes each retitle ask you first. A
mid-tier model is enough. Then type:

```
/monitor
```

It replies with the board as a table. From then on it runs by itself, and
prints the table again each time a row changes.

### 8. Open the board

Open the board's link in a browser window and leave it beside the app. Each
row says what the session needs and, to the right in a wide window or
underneath in a slim one, where it stands. Its session titles are links that
jump to the session. They work from a browser, and do nothing in the app's own
page viewer.

### Check that it works

- The board lists every session in your `Active` group within a minute.
- End a turn in one of them with a question for you. Its row moves to "on you"
  with the question within about 20 seconds.
- `ls "$HOME/<project>/.session-monitor/hooks"` shows one file per session that
  has ended a turn since step 5.
- The monitor's sidebar title reads like `1 on you · 12:40 · Monitor`.

If the board shows "Stale", the monitor has stopped checking: type `/monitor`
in it.

## Using it

| You type, in the monitor session | What happens |
|---|---|
| `/monitor` | Re-checks every session and prints the board. Starts the monitor if it stopped. |
| `/monitor done <part of a title>` | Sets that session's row aside until the session next speaks. |
| `/monitor stop` | Stops the monitor. The board says so. |

### A board inside the app

The app does not let a web page open a session, so the board page's links only
work from a browser. Chat links do work in the app, and the monitor prints the
board in chat each time a row changes. To keep a clickable board inside the
app, open the monitor session in its own window (right-click it in the
sidebar): each title in its latest table is a link that switches the main
window to that session.

## Updating

After a `git pull`, repeat step 4 and type `/monitor` in the monitor session,
which reloads the procedure.

## Uninstalling

Copy `~/.claude/settings.json.before-monitor` back over
`~/.claude/settings.json`, delete `~/.claude/skills/monitor`, and stop the
poller.

## Limits

- A permission-prompt row can stay up while a command you approved is still
  running: the hook learns a prompt was answered only when its tool finishes.
- A session added to `Active` appears, and one archived or moved out of it
  leaves, at the monitor's next wake: the next time any watched session
  changes, or up to about 75 minutes away. `/monitor` does it at once.
- A session's own scheduled loop tick is recorded as a reply from you, so it
  can clear that session's open question from the board until it asks again.
- The app shows its usual "task finished" banner when the monitor ends a turn
  while the app is in the background. The app has no per-session mute.

## Tests

```bash
/usr/bin/python3 -m unittest discover -s tests
```

They use the system interpreter because the hook runs under it.
