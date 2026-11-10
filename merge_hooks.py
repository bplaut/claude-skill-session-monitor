#!/usr/bin/python3
"""Add the session monitor's hook entries to a Claude Code settings file.

usage: merge_hooks.py <path to settings.json>

It appends each entry of settings-hooks.json (beside this script) to the end
of that event's list, leaving every existing entry where it is, and skips an
entry that is already there, so running it twice changes nothing. The file as
it was before the first run is kept beside it as <path>.before-monitor.

A settings file that does not parse is left untouched: Claude Code would be
ignoring it already, and guessing at a repair could drop other hooks.
"""

import json
import os
import shutil
import sys


def main(argv):
    if len(argv) != 2:
        sys.stderr.write(__doc__)
        return 2
    path = argv[1]
    with open(path) as f:
        text = f.read()
    try:
        settings = json.loads(text)
    except ValueError as error:
        sys.stderr.write("merge_hooks: %s does not parse (%s); nothing changed\n" % (path, error))
        return 1
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks", {}), dict):
        sys.stderr.write("merge_hooks: %s is not a settings object with a hooks object; nothing changed\n" % path)
        return 1

    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings-hooks.json")) as f:
        wanted = json.load(f)["hooks"]

    added = []
    hooks = settings.setdefault("hooks", {})
    for event, groups in wanted.items():
        existing = hooks.setdefault(event, [])
        if not isinstance(existing, list):
            sys.stderr.write("merge_hooks: hooks.%s in %s is not a list; nothing changed\n" % (event, path))
            return 1
        for group in groups:
            if group not in existing:
                existing.append(group)
                added.append(event)

    if not added:
        print("merge_hooks: every entry is already in %s; nothing changed" % path)
        return 0
    backup = path + ".before-monitor"
    if not os.path.exists(backup):
        shutil.copyfile(path, backup)
    scratch = "%s.%d.tmp" % (path, os.getpid())
    with open(scratch, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    os.replace(scratch, path)
    print("merge_hooks: added entries for %s; the previous file is %s" % (", ".join(added), backup))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
